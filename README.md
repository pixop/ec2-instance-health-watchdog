# EC2 Health Watchdog (External)

Simple, production-minded Python watchdog that runs on a **separate EC2 instance** and monitors a target EC2 instance using AWS EC2 health/status signals only.

It is intentionally scoped to EC2/OS-level recovery and does **not** inspect the Pixop application.

## Scope and Intent

This watchdog:

- polls `describe_instance_status` for one target instance
- reads:
  - `InstanceState.Name`
  - `SystemStatus.Status`
  - `InstanceStatus.Status`
- reboots only when instance-level impairment persists long enough and cooldown allows it
- keeps running through transient AWS API errors
- logs every decision path clearly

This watchdog intentionally does **not**:

- call the app API
- call `/health`
- inspect `systemctl`
- check whether `pixop-live` is running
- SSH into the target instance

## Two-Layer Recovery Model

Layer 1: local watchdog on target EC2  
handles `pixop-live` application failures and restarts the app

Layer 2: external EC2 watchdog (this project)  
handles EC2 instance/OS-level impairment and can reboot the instance

## Project Layout

```text
ec2_watchdog/
    watchdog.py
requirements.txt
README.md
iam-policy-example.json
scripts/
    install-systemd.sh
systemd/
    ec2-watchdog.service
```

## Configuration

Environment variables:

Required:

- `AWS_REGION`
- `TARGET_INSTANCE_ID`

Optional:

- `CHECK_INTERVAL_SECONDS` (default: `30`)
- `IMPAIRED_THRESHOLD_SECONDS` (default: `300`)
- `REBOOT_COOLDOWN_SECONDS` (default: `1800`)
- `LOG_LEVEL` (default: `INFO`)
- `REBOOT_ON_SYSTEM_STATUS_IMPAIRED` (default: `false`)

## Reboot Decision Rules

Default reboot trigger:

1. target `InstanceState.Name == "running"`
2. `InstanceStatus.Status == "impaired"`
3. impairment has persisted continuously for at least `IMPAIRED_THRESHOLD_SECONDS`
4. cooldown `REBOOT_COOLDOWN_SECONDS` has expired

Additional behavior:

- non-running states (`pending`, `stopping`, `stopped`, `shutting-down`, `terminated`) never trigger reboot
- impairment timers reset when state is not `running`
- `SystemStatus.Status == "impaired"` is logged clearly
- by default, system-status impairment does not trigger reboot (`REBOOT_ON_SYSTEM_STATUS_IMPAIRED=false`)
- if `REBOOT_ON_SYSTEM_STATUS_IMPAIRED=true`, persistent system-status impairment can also trigger reboot using the same threshold/cooldown guardrails

Monotonic time (`time.monotonic()`) is used for threshold/cooldown calculations.

## Startup Self-Test

On startup, the watchdog validates:

1. target instance can be described (`DescribeInstances`)
2. reboot permission with dry-run:

```python
ec2.reboot_instances(InstanceIds=[target_instance_id], DryRun=True)
```

Interpretation:

- `DryRunOperation`: permissions are valid
- `UnauthorizedOperation` (or auth failure): permission missing, process exits
- any other error: process exits with clear log

## IAM Permissions

Minimum practical permissions:

- `ec2:DescribeInstances`
- `ec2:DescribeInstanceStatus`
- `ec2:RebootInstances`

Example policy is included in `iam-policy-example.json`.

Notes:

- EC2 `Describe*` actions generally require `"Resource": "*"`
- restrict reboot permissions as much as practical (region/account/tags/instance targeting)
- depending on your policy model and AWS condition support, tag-based controls can be used to limit reboots

## Install and Run

### 1) Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) Export environment variables

```bash
export AWS_REGION=eu-central-1
export TARGET_INSTANCE_ID=i-0123456789abcdef0
export CHECK_INTERVAL_SECONDS=30
export IMPAIRED_THRESHOLD_SECONDS=300
export REBOOT_COOLDOWN_SECONDS=1800
export LOG_LEVEL=INFO
export REBOOT_ON_SYSTEM_STATUS_IMPAIRED=false
```

### 3) Run manually

```bash
python -m ec2_watchdog.watchdog
```

If startup dry-run permission check fails, fix IAM before running as a service.

## systemd Service Installation (Example)

The provided unit file is `systemd/ec2-watchdog.service`.

### Recommended (automated installer)

Use the installer script (run as root). It handles:

- creating service user/group
- copying project to `/opt/ec2-watchdog`
- setting ownership so virtualenv creation works
- creating `/etc/ec2-watchdog/ec2-watchdog.env` from `.env.example` if missing
- installing dependencies into `/opt/ec2-watchdog/.venv`
- installing and starting the systemd unit

```bash
chmod +x scripts/install-systemd.sh
sudo ./scripts/install-systemd.sh
```

### Manual installation

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin ec2-watchdog || true
sudo mkdir -p /opt/ec2-watchdog /etc/ec2-watchdog
sudo cp -r . /opt/ec2-watchdog
sudo chown -R ec2-watchdog:ec2-watchdog /opt/ec2-watchdog
sudo -u ec2-watchdog python3 -m venv /opt/ec2-watchdog/.venv
sudo -u ec2-watchdog /opt/ec2-watchdog/.venv/bin/pip install -r /opt/ec2-watchdog/requirements.txt
```

Create `/etc/ec2-watchdog/ec2-watchdog.env`:

```bash
AWS_REGION=eu-central-1
TARGET_INSTANCE_ID=i-0123456789abcdef0
CHECK_INTERVAL_SECONDS=30
IMPAIRED_THRESHOLD_SECONDS=300
REBOOT_COOLDOWN_SECONDS=1800
LOG_LEVEL=INFO
REBOOT_ON_SYSTEM_STATUS_IMPAIRED=false
```

Install and start service:

```bash
sudo cp systemd/ec2-watchdog.service /etc/systemd/system/ec2-watchdog.service
sudo systemctl daemon-reload
sudo systemctl enable --now ec2-watchdog.service
sudo systemctl status ec2-watchdog.service
```

### Common setup errors

If you see:

- `Permission denied: '/opt/ec2-watchdog/.venv'`
- `.venv/bin/activate: No such file or directory`
- `pip: command not found`

Then `/opt/ec2-watchdog` ownership is incorrect or the venv was never created.
Fix with:

```bash
sudo chown -R ec2-watchdog:ec2-watchdog /opt/ec2-watchdog
sudo -u ec2-watchdog python3 -m venv /opt/ec2-watchdog/.venv
sudo -u ec2-watchdog /opt/ec2-watchdog/.venv/bin/pip install -r /opt/ec2-watchdog/requirements.txt
```

## Operational Limitations

This external watchdog can detect/respond to:

- EC2 instance status check impairment
- likely guest OS or instance-level problems surfaced by AWS status checks
- persistent impaired instance status beyond configured threshold

This external watchdog does **not** detect/respond to:

- `pixop-live` deadlocks
- `/health` endpoint failure
- GPU pipeline stalls while EC2 status remains OK
- bad output quality
- application-level maintenance or intentional disablement
- degraded-but-not-dead performance

Those are intentionally out of scope for this external watchdog and should be handled by:

- local watchdog on target instance
- application metrics/alerts
- other service-level observability and response tooling
