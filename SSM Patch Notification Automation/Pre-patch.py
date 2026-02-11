import boto3
import os
import csv
import io
import html
from collections import Counter
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

#1. to read account ids, role_name and region from local csv file
def read_csv_local(file_name):
    rows = []
    file_path = os.path.join(os.path.dirname(__file__), file_name)

    with open(file_path, mode="r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    return rows

#2. checks for Maintenance windows scheduled for current date
def runs_today(next_execution_time):

    #  Normalize to datetime
    if isinstance(next_execution_time, str):
        next_execution_time = datetime.fromisoformat(
            next_execution_time.replace("Z", "+00:00")
        )

    #  Ensure timezone-aware
    if next_execution_time.tzinfo is None:
        next_execution_time = next_execution_time.replace(tzinfo=timezone.utc)

    #  Today's UTC window
    now_utc = datetime.now(timezone.utc)
    start_of_today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_today = start_of_today + timedelta(days=1)

    # print(f"NextExecutionTime: {next_execution_time}")
    # print(f"UTC Window: {start_of_today} → {end_of_today}")

    return start_of_today <= next_execution_time < end_of_today


# 3.Gets the target instance count per Maintenance window
def get_target_count(ssm, ec2, resourcegroups, window_id):

    targets_response = ssm.describe_maintenance_window_targets(
        WindowId=window_id
    )

    total = 0

    for mw_target in targets_response["Targets"]:

        # Collect tag filters PER MW TARGET
        tag_filters = []

        for rule in mw_target.get("Targets", []):
            key = rule["Key"]
            values = rule["Values"]

            # Case 1: Explicit Instance IDs
            if key == "InstanceIds":
                total += len(values)

            # Case 2: Tag-based targets
            elif key.startswith("tag:"):
                tag_key = key.split("tag:")[1]
                tag_filters.append({
                    "Name": f"tag:{tag_key}",
                    "Values": values
                })

            # Case 3: Resource Groups
            elif key == "resource-groups:Name":
                for group_name in values:

                    paginator = resourcegroups.get_paginator(
                        "list_group_resources"
                    )

                    count = 0
                    for page in paginator.paginate(Group=group_name):
                        for resource in page["ResourceIdentifiers"]:
                            # Only count EC2 instances
                            if resource["ResourceType"] == "AWS::EC2::Instance":
                                count += 1

                    total += count

            else:
                print(f"Unsupported target rule: {key}")

        # Resolve tag-based targets for THIS MW TARGET ONLY
        if tag_filters:
            paginator = ec2.get_paginator("describe_instances")
            instance_ids = set()

            for page in paginator.paginate(Filters=tag_filters):
                for reservation in page["Reservations"]:
                    for instance in reservation["Instances"]:
                        instance_ids.add(instance["InstanceId"])

            total += len(instance_ids)

    #print(f"Resolved target count = {total}")
    return total

#4.Write CSV to S3
def write_csv_to_s3(s3, bucket, key, rows):

    buffer = io.StringIO()
    fieldnames = rows[0].keys()

    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=buffer.getvalue()
    )
    #print(f"data is written to s3 bucket")

#5. To create html file
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

    # To count how many rows each AccountId has
    account_counts = Counter(row["AccountId"] for row in output_rows)

    html_body = """
    <html>
    <body>
      <p>Hello Team,</p>
      <p>Below are the Maintenance Windows running today:</p>

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

#6.loads account id details from which email will be sent
def load_email_config(csv_file):
    with open(csv_file, newline="") as f:
        reader = csv.DictReader(f)
        row = next(reader)   
        return {
            "account_id": row["account_id"],
            "role_name": row["role_name"],
            "region": row["region"]
        }

#7. Assume role into target account
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


# Main Lambda logic
def lambda_handler(event, context):

    OUTPUT_BUCKET = "mmpatching-custom-patchbaseline-dev"
    OUTPUT_KEY = "pre_patch_notification/mw-running-today-output.csv"

    EMAIL_FROM = "bhaskar.dm@modmed.com"
    EMAIL_TO = ["bhaskar.dm@modmed.com"]
    EMAIL_REGION = "us-east-1"

    pre_patch_rows = read_csv_local("accounts.csv")

    output_rows = []

    for row in pre_patch_rows:
        account_id = row["account_id"]
        role_name = row["role_name"]
        region = row["region"]

        #debug
        #print(f"account_id: {account_id}, role_name:{role_name}, region: {region}")

        #to assume the role
        session = assume_role(account_id, role_name, region)
        # Debug: confirm assumed account
        # sts = session.client("sts")
        # identity = sts.get_caller_identity()
        # print("Assumed identity:", identity)

        ssm = session.client("ssm")
        ec2 = session.client("ec2")
        s3  = session.client("s3")
        resourcegroups = session.client("resource-groups")
        response = ssm.describe_maintenance_windows()


        for mw in response["WindowIdentities"]:
            # if not mw.get("Enabled", True):
            #     continue

            # Some MWs may not have future executions
            if "NextExecutionTime" not in mw:
                continue

            #Excluding MWs not starting with "mmpatching"
            if not mw.get("Name", "").startswith("mmpatching"):
                continue

            if runs_today(mw["NextExecutionTime"]):
                target_count = get_target_count(ssm,ec2,resourcegroups,mw["WindowId"])

                output_rows.append({
                    "AccountId": account_id,
                    "Region": region,
                    "RoleName": role_name,
                    "MaintenanceWindowId": mw["WindowId"],
                    "MaintenanceWindowName": mw["Name"],
                    "TargetInstanceCount": target_count
                })


    # Handle no data case Skip CSV + Email if no Maintenance Windows matched
    if not output_rows:
        print("No Maintenance Windows running today. Skipping notification.")
        return {
            "status": "success",
            "records_written": 0,
            "message": "No MWs found, notification skipped"
        }


    #to write output 
    EMAIL_CONFIG_FILE = "email_account.csv"
    email_cfg = load_email_config(EMAIL_CONFIG_FILE)

    session = assume_role(
    account_id=email_cfg["account_id"],
    role_name=email_cfg["role_name"],
    region=email_cfg["region"]
    )

    # Write CSV to S3 
    write_csv_to_s3(s3=s3,bucket=OUTPUT_BUCKET,key=OUTPUT_KEY,rows=output_rows)

    # Building html
    html_body = build_html_table(output_rows)

    #To send consolidated pre patch notifcation email, considering those account details separately in 
    #email_accounts.csv
    send_email_ses(
    session=session,
    subject="Maintenance Windows Running Today",
    html_body=html_body,
    sender=EMAIL_FROM,
    recipients=EMAIL_TO,
    region=email_cfg["region"]
    )

    return {
        "status": "success",
        "records_written": len(output_rows)
    }

if __name__ == "__main__":
    print("Running script locally")
    result = lambda_handler({}, None)
    print(" Finished:", result)
