# EC2 Health Watchdog (External)

External EC2 watchdog that runs on a separate instance and uses only AWS EC2
status signals to decide when to reboot a target instance.

Python requirement: **3.10+**.

## What It Does

- polls `DescribeInstanceStatus` for one target instance
- reads `InstanceState.Name`, `InstanceStatus.Status`, `SystemStatus.Status`
- reboots only after **continuous** `InstanceStatus=impaired` for a threshold
- enforces reboot cooldown to avoid reboot loops
- performs startup self-test (`DescribeInstances` + reboot dry-run permission)
- keeps running through transient AWS API errors

## What It Does Not Do

- does not call `/health`
- does not check `pixop-live`, `systemctl`, logs, process state, or SSH
- does not infer application health from runtime behavior

This watchdog is Layer 2 only (EC2/OS recovery). Layer 1 app recovery should be
handled by a local watchdog on the target host.

## Configuration

Required:

- `AWS_REGION`
- `TARGET_INSTANCE_ID`

Optional defaults:

- `CHECK_INTERVAL_SECONDS=30`
- `IMPAIRED_THRESHOLD_SECONDS=300`
- `REBOOT_COOLDOWN_SECONDS=1800`
- `LOG_LEVEL=INFO`
- `REBOOT_ON_SYSTEM_STATUS_IMPAIRED=false`

Reboot condition (default behavior):

1. instance state is `running`
2. `InstanceStatus.Status == impaired`
3. impairment duration >= `IMPAIRED_THRESHOLD_SECONDS`
4. cooldown expired

`SystemStatus=impaired` is logged; reboot on system impairment is optional and
disabled by default.

## Local Run

```bash
cp .env.example .env
./run-local.sh
```

Or run manually:

```bash
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
source .env
python3 -m ec2_watchdog.watchdog
```

## IAM

Minimum actions:

- `ec2:DescribeInstances`
- `ec2:DescribeInstanceStatus`
- `ec2:RebootInstances`

- Example policy: `iam-policy-example.json`
- Full IAM setup guide: `IAM_SETUP.md`

## systemd (Recommended)

Use the installer:

```bash
sudo ./scripts/install-systemd.sh
```

This installs to `/opt/ec2-watchdog`, creates `/etc/ec2-watchdog/ec2-watchdog.env`
if needed, creates the venv with Python 3.10+, and enables
`ec2-watchdog.service`.

Useful commands:

```bash
sudo systemctl status ec2-watchdog
sudo journalctl -u ec2-watchdog -f
```

If you hit venv permission errors, fix ownership and recreate venv as the
service user:

```bash
sudo chown -R ec2-watchdog:ec2-watchdog /opt/ec2-watchdog
sudo -u ec2-watchdog python3.10 -m venv /opt/ec2-watchdog/.venv
sudo -u ec2-watchdog /opt/ec2-watchdog/.venv/bin/pip install -r /opt/ec2-watchdog/requirements.txt
```

## Limits

Detects/responds to:

- EC2 instance status check impairment
- persistent guest/instance-level problems surfaced by AWS checks

Does not detect/respond to:

- app deadlocks, `/health` failures, or service-level quality/performance issues

Use local watchdog + app observability for those.
