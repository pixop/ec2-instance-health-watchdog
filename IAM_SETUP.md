# EC2 Watchdog IAM Setup

This document explains how to create and attach the IAM permissions required by the external EC2 watchdog.

The watchdog runs on a separate EC2 instance and monitors the AWS EC2 status of a target instance. If the target instance's EC2 **instance status check** remains impaired for long enough, the watchdog can reboot the target instance.

The watchdog does **not** monitor the Pixop application directly. It does not call `/health`, inspect `pixop-live`, use SSH, or check `systemd` service state.

## Recovery Model

The intended recovery model is two-layered:

```text
Layer 1: local watchdog on the target EC2 instance
    Handles pixop-live crashes, application hangs, and local health failures.
    Action: restart the Pixop application.

Layer 2: external EC2 watchdog on a separate EC2 instance
    Handles EC2 instance status impairment.
    Action: reboot the target EC2 instance after a conservative threshold.
```

This IAM setup is only for **Layer 2**.

## What the Watchdog Needs Permission To Do

The external watchdog needs to:

1. Read EC2 instance status.
2. Read EC2 instance metadata/tags/state.
3. Reboot the target EC2 instance.

Required IAM actions:

```text
ec2:DescribeInstances
ec2:DescribeInstanceStatus
ec2:RebootInstances
```

The `Describe*` actions generally require `"Resource": "*"`. The reboot permission should be restricted as much as practical.

This repository includes both:

* `iam-policy-example.json` (tag-gated reboot style), and
* this guide's target-instance ARN style.

Use the style that best fits your IAM controls.

## Assumptions

This guide assumes:

* The watchdog runs on its own EC2 instance.
* The target instance is the EC2 instance that may need to be rebooted.
* The watchdog instance will use an IAM role via an EC2 instance profile.
* No long-lived AWS access keys are stored on the watchdog instance.

Set these variables locally before running the commands:

```bash
export AWS_REGION="eu-west-1"
export AWS_ACCOUNT_ID="123456789012"

# The instance that should be rebooted if EC2 instance status is impaired
export TARGET_INSTANCE_ID="i-xxxxxxxxxxxxxxxxx"

# The instance where the external watchdog runs
export WATCHDOG_INSTANCE_ID="i-yyyyyyyyyyyyyyyyy"

export POLICY_NAME="PixopEc2WatchdogPolicy"
export ROLE_NAME="PixopEc2WatchdogRole"
export INSTANCE_PROFILE_NAME="PixopEc2WatchdogInstanceProfile"
```

Verify the active AWS identity:

```bash
aws sts get-caller-identity
```

## Step 1: Create the IAM Policy Document

Create `iam-policy.json`:

```bash
cat > iam-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DescribeEc2InstancesAndStatus",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus"
      ],
      "Resource": "*"
    },
    {
      "Sid": "AllowRebootOfTargetInstanceOnly",
      "Effect": "Allow",
      "Action": "ec2:RebootInstances",
      "Resource": "arn:aws:ec2:${AWS_REGION}:${AWS_ACCOUNT_ID}:instance/${TARGET_INSTANCE_ID}"
    }
  ]
}
EOF
```

This grants:

* read-only EC2 describe permissions
* reboot permission for the specific target instance only

If your IAM model requires broader resource scope for EC2 write actions, use a
tag-gated reboot policy (see the optional section below).

## Step 2: Create the IAM Policy

Create the managed IAM policy:

```bash
aws iam create-policy \
  --policy-name "$POLICY_NAME" \
  --policy-document file://iam-policy.json
```

Export the policy ARN:

```bash
export POLICY_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:policy/${POLICY_NAME}"
```

If the policy already exists, update it by creating a new policy version:

```bash
aws iam create-policy-version \
  --policy-arn "$POLICY_ARN" \
  --policy-document file://iam-policy.json \
  --set-as-default
```

IAM managed policies can only have a limited number of versions. If version creation fails because there are too many versions, list the existing versions:

```bash
aws iam list-policy-versions \
  --policy-arn "$POLICY_ARN"
```

Then delete an old non-default version:

```bash
aws iam delete-policy-version \
  --policy-arn "$POLICY_ARN" \
  --version-id v1
```

Do not delete the current default version.

## Step 3: Create the EC2 Trust Policy

Create `trust-policy.json`:

```bash
cat > trust-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowEc2ToAssumeRole",
      "Effect": "Allow",
      "Principal": {
        "Service": "ec2.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
```

This allows EC2 to assume the IAM role on behalf of the watchdog instance.

## Step 4: Create the IAM Role

Create the role:

```bash
aws iam create-role \
  --role-name "$ROLE_NAME" \
  --assume-role-policy-document file://trust-policy.json
```

Attach the watchdog policy to the role:

```bash
aws iam attach-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-arn "$POLICY_ARN"
```

## Step 5: Create the Instance Profile

Create the instance profile:

```bash
aws iam create-instance-profile \
  --instance-profile-name "$INSTANCE_PROFILE_NAME"
```

Add the role to the instance profile:

```bash
aws iam add-role-to-instance-profile \
  --instance-profile-name "$INSTANCE_PROFILE_NAME" \
  --role-name "$ROLE_NAME"
```

IAM changes can take a short time to propagate. If the next step fails immediately, wait a minute and retry.

## Step 6: Attach the Instance Profile to the Watchdog EC2 Instance

Attach the instance profile to the watchdog instance:

```bash
aws ec2 associate-iam-instance-profile \
  --region "$AWS_REGION" \
  --instance-id "$WATCHDOG_INSTANCE_ID" \
  --iam-instance-profile Name="$INSTANCE_PROFILE_NAME"
```

If the watchdog instance already has an instance profile, association may fail.
In that case, replace it with `replace-iam-instance-profile-association` after
retrieving the current association ID:

```bash
aws ec2 describe-iam-instance-profile-associations \
  --region "$AWS_REGION" \
  --filters Name=instance-id,Values="$WATCHDOG_INSTANCE_ID" \
  --query 'IamInstanceProfileAssociations[0].AssociationId' \
  --output text

# Replace <association-id> with the value from the previous command.
aws ec2 replace-iam-instance-profile-association \
  --region "$AWS_REGION" \
  --association-id <association-id> \
  --iam-instance-profile Name="$INSTANCE_PROFILE_NAME"
```

Verify the attachment:

```bash
aws ec2 describe-instances \
  --region "$AWS_REGION" \
  --instance-ids "$WATCHDOG_INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].IamInstanceProfile'
```

## Step 7: Verify Credentials from the Watchdog Instance

SSH into the watchdog EC2 instance and run:

```bash
aws sts get-caller-identity
```

Expected result should show an assumed role identity for the watchdog role.

Example:

```text
arn:aws:sts::123456789012:assumed-role/PixopEc2WatchdogRole/i-yyyyyyyyyyyyyyyyy
```

## Step 8: Verify Describe Permissions

From the watchdog EC2 instance, run:

```bash
aws ec2 describe-instance-status \
  --region "$AWS_REGION" \
  --instance-ids "$TARGET_INSTANCE_ID" \
  --include-all-instances \
  --query 'InstanceStatuses[0].{State:InstanceState.Name,System:SystemStatus.Status,Instance:InstanceStatus.Status}' \
  --output table
```

Expected healthy output looks similar to:

```text
------------------------------
|   DescribeInstanceStatus   |
+----------+---------+-------+
| Instance | ok      |
| State    | running |
| System   | ok      |
+----------+---------+-------+
```

The watchdog should primarily care about:

```text
InstanceStatus.Status == impaired
```

A persistent impaired instance status is the signal that the watchdog may use to reboot the target instance.

## Step 9: Verify Reboot Permission Without Rebooting

Use EC2 dry-run mode:

```bash
aws ec2 reboot-instances \
  --region "$AWS_REGION" \
  --instance-ids "$TARGET_INSTANCE_ID" \
  --dry-run
```

If permissions are correct, AWS intentionally returns an error like:

```text
An error occurred (DryRunOperation) when calling the RebootInstances operation:
Request would have succeeded, but DryRun flag is set.
```

This means the watchdog has permission to reboot the target instance, but no reboot was performed.

If permissions are missing, the response will look like:

```text
An error occurred (UnauthorizedOperation) when calling the RebootInstances operation:
You are not authorized to perform this operation.
```

You may also see `AuthFailure` in some environments. The watchdog treats both
`UnauthorizedOperation` and `AuthFailure` as missing reboot permission.

The watchdog performs this dry-run check at startup and refuses to run if it
does not receive `DryRunOperation`.

## Optional: Use a Tag-Gated Reboot Policy

As an extra safety measure, tag the target instance:

```bash
aws ec2 create-tags \
  --region "$AWS_REGION" \
  --resources "$TARGET_INSTANCE_ID" \
  --tags Key=PixopExternalWatchdogTarget,Value=true
```

Then replace the reboot statement in `iam-policy.json` with a tag-gated version:

```json
{
  "Sid": "AllowRebootOfWatchdogTaggedInstancesOnly",
  "Effect": "Allow",
  "Action": "ec2:RebootInstances",
  "Resource": "arn:aws:ec2:eu-west-1:123456789012:instance/*",
  "Condition": {
    "StringEquals": {
      "ec2:ResourceTag/PixopExternalWatchdogTarget": "true"
    }
  }
}
```

Adjust the region and account ID as needed.

Then update the policy:

```bash
aws iam create-policy-version \
  --policy-arn "$POLICY_ARN" \
  --policy-document file://iam-policy.json \
  --set-as-default
```

Run the dry-run reboot test again:

```bash
aws ec2 reboot-instances \
  --region "$AWS_REGION" \
  --instance-ids "$TARGET_INSTANCE_ID" \
  --dry-run
```

Expected successful result:

```text
DryRunOperation
```

## Recommended Watchdog Environment

The watchdog application should use environment variables similar to:

```bash
AWS_REGION=eu-west-1
TARGET_INSTANCE_ID=i-xxxxxxxxxxxxxxxxx
CHECK_INTERVAL_SECONDS=30
IMPAIRED_THRESHOLD_SECONDS=300
REBOOT_COOLDOWN_SECONDS=1800
LOG_LEVEL=INFO
REBOOT_ON_SYSTEM_STATUS_IMPAIRED=false
```

Python runtime requirement for this project is **Python 3.10+**.

Recommended behavior:

```text
Reboot only if:

1. Target instance is running
2. EC2 InstanceStatus.Status == impaired
3. The impaired state has persisted continuously for the configured threshold
4. Reboot cooldown has expired
5. Dry-run permission self-test passed at startup
```

The watchdog should not reboot for a single failed check.

## What the External Watchdog Should Not Do

The external watchdog should not:

* call `/health`
* inspect `pixop-live`
* use SSH
* check `systemctl status pixop-live`
* check application logs
* check process state
* infer anything from whether the Pixop application is running

Application-level recovery is handled by the local watchdog on the target instance.

## What This Setup Detects

This setup is intended to detect and respond to:

* EC2 instance status check impairment
* likely guest OS or instance-level health issues reported by AWS
* persistent impaired instance status after the configured threshold

Typical response:

```text
InstanceStatus.Status == impaired for 5 minutes
    -> reboot target EC2 instance
```

## What This Setup Does Not Detect

This setup intentionally does not detect or respond to:

* `pixop-live` application deadlocks
* `/health` endpoint failure
* GPU pipeline stalls while EC2 status remains OK
* bad output quality
* application-level maintenance
* deliberate service disablement
* degraded-but-not-dead performance

Those are outside the scope of the external EC2 watchdog.

## Cleanup

Detach the policy from the role:

```bash
aws iam detach-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-arn "$POLICY_ARN"
```

Remove the role from the instance profile:

```bash
aws iam remove-role-from-instance-profile \
  --instance-profile-name "$INSTANCE_PROFILE_NAME" \
  --role-name "$ROLE_NAME"
```

Delete the instance profile:

```bash
aws iam delete-instance-profile \
  --instance-profile-name "$INSTANCE_PROFILE_NAME"
```

Delete the role:

```bash
aws iam delete-role \
  --role-name "$ROLE_NAME"
```

Delete the policy:

```bash
aws iam delete-policy \
  --policy-arn "$POLICY_ARN"
```

## Final Validation Checklist

Before enabling automatic reboots, verify:

* [ ] The watchdog EC2 instance has the IAM instance profile attached.
* [ ] `aws sts get-caller-identity` works from the watchdog instance.
* [ ] `describe-instance-status` works for the target instance.
* [ ] `reboot-instances --dry-run` returns `DryRunOperation`.
* [ ] The watchdog has a conservative impairment threshold.
* [ ] The watchdog has a reboot cooldown.
* [ ] The watchdog does not inspect application health.
* [ ] The local Pixop watchdog handles application-level recovery.
