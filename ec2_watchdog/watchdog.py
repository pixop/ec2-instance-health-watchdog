"""External EC2 instance watchdog.

This watchdog runs on a separate EC2 instance and monitors AWS EC2
instance/system status checks for a target instance. It intentionally does not
inspect application health.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError


LOGGER = logging.getLogger("ec2_watchdog")

RUNNING_STATE = "running"
IMPAIRED_STATUS = "impaired"
MIN_PYTHON_VERSION = (3, 10)


class ConfigError(ValueError):
    """Raised when required config is missing or invalid."""


@dataclass(frozen=True)
class Config:
    aws_region: str
    target_instance_id: str
    check_interval_seconds: int = 30
    impaired_threshold_seconds: int = 300
    reboot_cooldown_seconds: int = 1800
    log_level: str = "INFO"
    reboot_on_system_status_impaired: bool = False


@dataclass
class InstanceHealth:
    instance_state: str
    system_status: str
    instance_status: str


def _get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _get_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got: {raw_value!r}") from exc

    if value <= 0:
        raise ConfigError(f"{name} must be > 0, got: {value}")
    return value


def _get_bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
    truthy = {"1", "true", "yes", "y", "on"}
    falsy = {"0", "false", "no", "n", "off"}
    if normalized in truthy:
        return True
    if normalized in falsy:
        return False
    raise ConfigError(
        f"{name} must be boolean-like (true/false/1/0), got: {raw_value!r}"
    )


def load_config() -> Config:
    """Load and validate config from environment variables."""
    return Config(
        aws_region=_get_required_env("AWS_REGION"),
        target_instance_id=_get_required_env("TARGET_INSTANCE_ID"),
        check_interval_seconds=_get_int_env("CHECK_INTERVAL_SECONDS", 30),
        impaired_threshold_seconds=_get_int_env("IMPAIRED_THRESHOLD_SECONDS", 300),
        reboot_cooldown_seconds=_get_int_env("REBOOT_COOLDOWN_SECONDS", 1800),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        reboot_on_system_status_impaired=_get_bool_env(
            "REBOOT_ON_SYSTEM_STATUS_IMPAIRED", False
        ),
    )


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), None)
    if not isinstance(level, int):
        raise ConfigError(f"Invalid LOG_LEVEL: {level_name!r}")

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def create_ec2_client(config: Config):
    """Create the boto3 EC2 client in the configured region."""
    return boto3.client("ec2", region_name=config.aws_region)


def dry_run_reboot_permission_check(ec2_client, target_instance_id: str) -> None:
    """Validate reboot permissions with DryRun=True.

    Expected behavior:
    - DryRunOperation => permissions are valid.
    - UnauthorizedOperation => missing permissions.
    """
    try:
        ec2_client.reboot_instances(InstanceIds=[target_instance_id], DryRun=True)
        # Most AWS APIs should not return success on dry-run, but keep behavior explicit.
        LOGGER.warning(
            "Dry-run reboot returned success unexpectedly; proceeding with caution."
        )
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "Unknown")
        if code == "DryRunOperation":
            LOGGER.info("Dry-run reboot permission check passed (DryRunOperation).")
            return
        if code in {"UnauthorizedOperation", "AuthFailure"}:
            raise PermissionError(
                "Dry-run reboot permission check failed: missing "
                "ec2:RebootInstances permission for target instance."
            ) from err
        raise RuntimeError(
            f"Unexpected error during dry-run reboot permission check: {code} - {err}"
        ) from err


def describe_target_instance(ec2_client, target_instance_id: str) -> None:
    """Fail fast if the target instance cannot be described."""
    try:
        response = ec2_client.describe_instances(InstanceIds=[target_instance_id])
    except (ClientError, BotoCoreError) as err:
        raise RuntimeError(
            f"Failed to describe target instance {target_instance_id}: {err}"
        ) from err

    reservations = response.get("Reservations", [])
    instances = [
        instance
        for reservation in reservations
        for instance in reservation.get("Instances", [])
    ]
    if not instances:
        raise RuntimeError(
            f"Target instance {target_instance_id} does not exist or is not visible "
            "to current IAM role."
        )

    LOGGER.info("Target instance %s description check passed.", target_instance_id)


def get_instance_health(ec2_client, target_instance_id: str) -> InstanceHealth:
    """Read state + status checks from describe_instance_status."""
    response = ec2_client.describe_instance_status(
        InstanceIds=[target_instance_id],
        IncludeAllInstances=True,
    )

    statuses = response.get("InstanceStatuses", [])
    if not statuses:
        # IncludeAllInstances=True should usually return data for existing instances.
        # If AWS returns empty status, treat as unknown and skip reboot decisions.
        return InstanceHealth(
            instance_state="unknown",
            system_status="unknown",
            instance_status="unknown",
        )

    status = statuses[0]
    instance_state = status.get("InstanceState", {}).get("Name", "unknown")
    system_status = status.get("SystemStatus", {}).get("Status", "unknown")
    instance_status = status.get("InstanceStatus", {}).get("Status", "unknown")
    return InstanceHealth(
        instance_state=instance_state,
        system_status=system_status,
        instance_status=instance_status,
    )


def _cooldown_remaining_seconds(
    now_monotonic: float, last_reboot_monotonic: Optional[float], cooldown_seconds: int
) -> float:
    if last_reboot_monotonic is None:
        return 0.0
    elapsed = now_monotonic - last_reboot_monotonic
    return max(0.0, cooldown_seconds - elapsed)


def should_reboot(
    *,
    now_monotonic: float,
    impaired_since_monotonic: Optional[float],
    last_reboot_monotonic: Optional[float],
    impaired_threshold_seconds: int,
    reboot_cooldown_seconds: int,
) -> tuple[bool, str]:
    """Decide whether reboot should be attempted for current impairment."""
    if impaired_since_monotonic is None:
        return False, "No active impairment timer."

    impaired_for = now_monotonic - impaired_since_monotonic
    if impaired_for < impaired_threshold_seconds:
        remaining = impaired_threshold_seconds - impaired_for
        return (
            False,
            "Impairment threshold not reached yet "
            f"(impaired_for={impaired_for:.1f}s, remaining={remaining:.1f}s).",
        )

    cooldown_remaining = _cooldown_remaining_seconds(
        now_monotonic, last_reboot_monotonic, reboot_cooldown_seconds
    )
    if cooldown_remaining > 0:
        return (
            False,
            "Reboot suppressed by cooldown "
            f"(remaining={cooldown_remaining:.1f}s).",
        )

    return True, "Impairment threshold reached and cooldown expired."


def reboot_instance(ec2_client, target_instance_id: str) -> bool:
    """Attempt to reboot target instance. Returns True on accepted request."""
    try:
        ec2_client.reboot_instances(InstanceIds=[target_instance_id])
        LOGGER.warning("Reboot API call submitted for instance %s.", target_instance_id)
        return True
    except (ClientError, BotoCoreError) as err:
        LOGGER.exception(
            "Failed to reboot target instance %s: %s", target_instance_id, err
        )
        return False


def main_loop(ec2_client, config: Config) -> None:
    instance_impaired_since: Optional[float] = None
    system_impaired_since: Optional[float] = None
    last_reboot_time: Optional[float] = None

    LOGGER.info(
        "Starting watchdog loop for instance %s in region %s. "
        "check_interval=%ss impaired_threshold=%ss cooldown=%ss "
        "reboot_on_system_status_impaired=%s",
        config.target_instance_id,
        config.aws_region,
        config.check_interval_seconds,
        config.impaired_threshold_seconds,
        config.reboot_cooldown_seconds,
        config.reboot_on_system_status_impaired,
    )

    while True:
        now = time.monotonic()
        try:
            health = get_instance_health(ec2_client, config.target_instance_id)
            LOGGER.info(
                "Observed state for %s: instance_state=%s system_status=%s "
                "instance_status=%s",
                config.target_instance_id,
                health.instance_state,
                health.system_status,
                health.instance_status,
            )

            if health.instance_state != RUNNING_STATE:
                if instance_impaired_since is not None or system_impaired_since is not None:
                    LOGGER.info(
                        "Instance state is %s (not running); resetting impairment timers.",
                        health.instance_state,
                    )
                instance_impaired_since = None
                system_impaired_since = None
                time.sleep(config.check_interval_seconds)
                continue

            if health.system_status == IMPAIRED_STATUS:
                if system_impaired_since is None:
                    system_impaired_since = now
                    LOGGER.warning("System status is impaired (timer started).")
                else:
                    LOGGER.warning(
                        "System status remains impaired for %.1fs.",
                        now - system_impaired_since,
                    )

                if not config.reboot_on_system_status_impaired:
                    LOGGER.info(
                        "System impairment does not trigger reboot by configuration "
                        "(REBOOT_ON_SYSTEM_STATUS_IMPAIRED=false)."
                    )
                else:
                    should, reason = should_reboot(
                        now_monotonic=now,
                        impaired_since_monotonic=system_impaired_since,
                        last_reboot_monotonic=last_reboot_time,
                        impaired_threshold_seconds=config.impaired_threshold_seconds,
                        reboot_cooldown_seconds=config.reboot_cooldown_seconds,
                    )
                    LOGGER.info("System impairment reboot decision: %s", reason)
                    if should and reboot_instance(ec2_client, config.target_instance_id):
                        last_reboot_time = now
                        system_impaired_since = None
                        instance_impaired_since = None
                        time.sleep(config.check_interval_seconds)
                        continue
            else:
                system_impaired_since = None

            if health.instance_status == IMPAIRED_STATUS:
                if instance_impaired_since is None:
                    instance_impaired_since = now
                    LOGGER.warning("Instance status is impaired (timer started).")

                should, reason = should_reboot(
                    now_monotonic=now,
                    impaired_since_monotonic=instance_impaired_since,
                    last_reboot_monotonic=last_reboot_time,
                    impaired_threshold_seconds=config.impaired_threshold_seconds,
                    reboot_cooldown_seconds=config.reboot_cooldown_seconds,
                )
                LOGGER.info("Instance impairment reboot decision: %s", reason)

                if should and reboot_instance(ec2_client, config.target_instance_id):
                    last_reboot_time = now
                    instance_impaired_since = None
                    system_impaired_since = None
            else:
                if instance_impaired_since is not None:
                    LOGGER.info(
                        "Instance status recovered from impaired to %s; resetting timer.",
                        health.instance_status,
                    )
                instance_impaired_since = None

        except (ClientError, BotoCoreError) as err:
            LOGGER.exception(
                "AWS API error while monitoring target instance %s: %s",
                config.target_instance_id,
                err,
            )
        except Exception:
            # Keep loop alive on unexpected runtime errors.
            LOGGER.exception("Unexpected watchdog loop error.")

        time.sleep(config.check_interval_seconds)


def main() -> int:
    if sys.version_info < MIN_PYTHON_VERSION:
        print(
            "Python 3.10+ is required. "
            f"Current version: {sys.version_info.major}.{sys.version_info.minor}",
            file=sys.stderr,
        )
        return 1

    try:
        config = load_config()
        configure_logging(config.log_level)
    except ConfigError as err:
        logging.basicConfig(level=logging.ERROR)
        LOGGER.error("Configuration error: %s", err)
        return 2

    LOGGER.info("Initializing EC2 watchdog.")
    ec2_client = create_ec2_client(config)

    try:
        describe_target_instance(ec2_client, config.target_instance_id)
        dry_run_reboot_permission_check(ec2_client, config.target_instance_id)
    except PermissionError as err:
        LOGGER.error("%s", err)
        return 3
    except Exception as err:
        LOGGER.error("Startup self-test failed: %s", err)
        return 4

    try:
        main_loop(ec2_client, config)
    except KeyboardInterrupt:
        LOGGER.info("Received interrupt; watchdog exiting.")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
