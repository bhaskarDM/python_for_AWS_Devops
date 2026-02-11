import boto3
import os
import csv
import json
import io
from collections import Counter
from datetime import datetime, timezone, timedelta
import html
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

# to load shared or email account details from email_account.csv
def load_shared_account(csv_file):
    with open(csv_file, newline="") as f:
        reader = csv.DictReader(f)
        row = next(reader)   
        return {
            "account_id": row["account_id"],
            "role_name": row["role_name"],
            "region": row["region"]
        }

# Assume Role
def assume_role(account_id, role_name, region):
    sts = boto3.client("sts")
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    response = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="MWCheckSession"
    )

    creds = response["Credentials"]

    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region
    )

# CSV Helpers from shared s3 bucket
def write_csv_to_s3(s3, bucket, key, rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

    s3.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())

# to read csv file
def read_csv_from_s3(s3, bucket, key):
    obj = s3.get_object(Bucket=bucket, Key=key)
    csv_data = obj["Body"].read().decode("utf-8")
    return list(csv.DictReader(io.StringIO(csv_data)))


# To get per instance status count 
def get_patch_status_counts(ssm, window_id):
    if not window_id.startswith("mw-"):
        print(f"Invalid WindowId skipped: {window_id}")
        return 0, 0
    else:
        print(f"WindowId: {window_id}")

    executions = ssm.describe_maintenance_window_executions(
        WindowId=window_id,
        MaxResults=10
    )["WindowExecutions"]

    if not executions:
        return 0, 0

    executions.sort(key=lambda x: x["StartTime"], reverse=True)
    execution_id = executions[0]["WindowExecutionId"]

    print(f"windows execution_id = {execution_id}")

    success = 0
    failure = 0

    tasks = ssm.describe_maintenance_window_execution_tasks(
        WindowExecutionId=execution_id
    )["WindowExecutionTaskIdentities"]

    print(f"tasks inside windows execution id: {tasks}")

    for task in tasks:
        if task.get("TaskArn") != "AWS-RunPatchBaseline":
            continue

        task_id = task["TaskExecutionId"]
        print(f"evaluating task_id: {task_id}")

        paginator = ssm.get_paginator(
            "describe_maintenance_window_execution_task_invocations"
        )

        for page in paginator.paginate(
            WindowExecutionId=execution_id,
            TaskId=task_id
        ):
            for inv in page["WindowExecutionTaskInvocationIdentities"]:

                # Parse Parameters to detect Install task
                raw_params = inv.get("Parameters", "")
                operation = []

                if isinstance(raw_params, str) and raw_params:
                    try:
                        parsed = json.loads(raw_params)
                        operation = (
                            parsed
                            .get("parameters", {})
                            .get("Operation", [])
                        )
                    except json.JSONDecodeError:
                        pass

                print(f"task_id {task_id} operation: {operation}")

                # Ignore SCAN
                if "Install" not in operation:
                    continue

                # Get CommandId (ExecutionId)
                command_id = inv.get("ExecutionId")
                print(f"INSTALL command_id: {command_id}")

                if not command_id:
                    continue

                #  Get per-instance results
                cmd_paginator = ssm.get_paginator("list_command_invocations")

                for cmd_page in cmd_paginator.paginate(
                    CommandId=command_id,
                    Details=False
                ):
                    for cmd_inv in cmd_page["CommandInvocations"]:
                        inst_status = cmd_inv.get("Status")
                        instance_id = cmd_inv.get("InstanceId")

                        print(f"Instance {instance_id} status: {inst_status}")

                        # Count per-instance result
                        if inst_status == "Success":
                            success += 1
                        elif inst_status in (
                            "Failed",
                            "TimedOut",
                            "Cancelled",
                            "ExecutionTimedOut"
                        ):
                            failure += 1

    return success, failure


# Build Html
def build_html_table(output_rows):
    if not output_rows:
        return """
        <html>
          <body>
            <p>No Maintenance Windows running today.</p>
          </body>
        </html>
        """

    # Define fields to exclude in Email
    excluded_fields = {"MaintenanceWindowId", "Region", "RoleName"}

    # Exclude additional fields dynamically
    headers = [h for h in output_rows[0].keys() if h not in excluded_fields]

    def prettify_header(header):
        return "".join(
            f" {c}" if c.isupper() else c for c in header
        ).strip()

    # Count how many rows each AccountId has
    account_counts = Counter(row["AccountId"] for row in output_rows)

    html_body = """
    <html>
    <body>
      <p>Hello Team,</p>
      <p>Below is the Post Patch Status of Maintenance windows:</p>

      <table border="1" cellpadding="6" cellspacing="0"
             style="border-collapse:collapse; font-family:Arial, sans-serif; font-size:13px;">
        <tr style="background-color:#f2f2f2; font-weight:bold;">
    """

    # Header row
    for header in headers:
        html_body += f"<th>{html.escape(prettify_header(header))}</th>"

    html_body += "</tr>"

    rendered_accounts = set()

    for row in output_rows:
        html_body += "<tr>"

        for header in headers:
            value = row.get(header, "")

            # Proper merge using rowspan
            if header == "AccountId":
                if value not in rendered_accounts:
                    rowspan = account_counts[value]
                    html_body += (
                        f"<td rowspan='{rowspan}' "
                        f"style='vertical-align:middle;'>"
                        f"{html.escape(str(value))}</td>"
                    )
                    rendered_accounts.add(value)
                # else: DO NOT render AccountId cell at all
            else:
                html_body += f"<td>{html.escape(str(value))}</td>"

        html_body += "</tr>"

    html_body += """
      </table>

      <br>
      <p>Regards,<br>Patch Automation</p>
    </body>
    </html>
    """

    return html_body

#8.SES
def send_email_ses(session, subject, html_body, sender, recipients, region):
    ses = session.client("ses", region_name=region)
    #ses = boto3.client("ses", region_name=region)

    response = ses.send_email(
        Source=sender,
        Destination={
            "ToAddresses": recipients
        },
        Message={
            "Subject": {
                "Data": subject,
                "Charset": "UTF-8"
            },
            "Body": {
                "Html": {
                    "Data": html_body,
                    "Charset": "UTF-8"
                }
            }
        }
    )

    return response


def post_patch_function():

    #shared account input bucket
    INPUT_BUCKET = "mmpatching-custom-patchbaseline-dev"

    #shared account file path where pre patch output is written
    PRE_PATCH_KEY = "pre_patch_notification/mw-running-today-output.csv"

    #csv in which post patch results are stored
    POST_PATCH_KEY = "post_patch_notification/mw-post-patch-output.csv"

    EMAIL_FROM = "bhaskar.dm@modmed.com"
    EMAIL_TO = ["bhaskar.dm@modmed.com"]
    EMAIL_REGION = "us-east-1" #currently SES is created in us-east-1 region

    #for now considering email_account.csv as shared account
    shared_account_row = load_shared_account("email_account.csv")

    session = assume_role(
        account_id=shared_account_row["account_id"],
        role_name=shared_account_row["role_name"],
        region=shared_account_row["region"])

    s3=session.client("s3")
    pre_patch_rows=read_csv_from_s3(s3, INPUT_BUCKET, PRE_PATCH_KEY)
    #print(pre_patch_rows)


    # Handle no data case Skip CSV + Email if no Maintenance Windows matched
    if not pre_patch_rows:
        print("No Maintenance Windows Scheduled for today. Skipping notification.")
        sys.exit(0) 

    post_patch_rows = []

    # Initialize cache variables before the loop
    cached_session = None
    cached_account_id = None
    cached_role_name = None
    cached_region = None

    for row in pre_patch_rows:
        account_id = row["AccountId"]
        role_name = row["RoleName"]
        region = row["Region"]
        window_id = row["MaintenanceWindowId"]
        window_name = row["MaintenanceWindowName"]

        # Assume role only if account / role / region changed
        if (cached_session is None or account_id != cached_account_id or role_name != cached_role_name
        or region != cached_region):
            cached_session = assume_role(account_id, role_name, region)
            cached_account_id = account_id
            cached_role_name = role_name
            cached_region = region

        # Reuse cached session
        session = cached_session

        #ssm = boto3.client("ssm", region_name="us-east-1")
        #session = assume_role(account_id, role_name, region)
        ssm = session.client("ssm")
        ec2 = session.client("ec2")
        s3  = session.client("s3")
        resourcegroups = session.client("resource-groups")

        success, failure = get_patch_status_counts(ssm, window_id)

        post_patch_rows.append({
            "AccountId": account_id,
            "Region": region,
            "RoleName": role_name,
            "MaintenanceWindowId": window_id,
            "MaintenanceWindowName": window_name,
            "TargetInstanceCount": row["TargetInstanceCount"],
            #"Status": f"Success - {success}, Failure - {failure}"
            "Success": success,
            "Failure": failure
        })  
    #debug
    #print(f"post patch rows: {post_patch_rows}")  

    #to write output to shared s3 bucket so assuming shared account role
    EMAIL_CONFIG_FILE = "email_account.csv"
    email_cfg = load_shared_account(EMAIL_CONFIG_FILE)

    session = assume_role(
    account_id=email_cfg["account_id"],
    role_name=email_cfg["role_name"],
    region=email_cfg["region"]
    )


    write_csv_to_s3(s3, INPUT_BUCKET, POST_PATCH_KEY, post_patch_rows)

    # Build html body
    html_body = build_html_table(post_patch_rows) 

    #send SES
    send_email_ses(
        session=session,
        subject="Post Patch Status Report",
        html_body=html_body,
        sender=EMAIL_FROM,
        recipients=EMAIL_TO,
        region=email_cfg["region"]
    )


if __name__ == "__main__":
    print("Running script locally")
    post_patch_function()
    print("Finished")