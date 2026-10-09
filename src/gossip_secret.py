"""Custom resource: the gossip HMAC key as an SSM SecureString.

Create writes a random 32-byte key to ParameterName (and leaves an existing one
alone, so a replaced resource keeps the key the running gateways hold). Delete
removes it. Update is a no-op. The gateways read it through
FLOW_GOSSIP_SECRET_SOURCE=ssm://<ParameterName>.
"""

import base64
import json
import secrets
import urllib.request

import boto3


def cfn_send(event, context, status, data):
    body = json.dumps({
        "Status": status,
        "Reason": "See " + context.log_stream_name,
        "PhysicalResourceId": event.get("PhysicalResourceId", context.log_stream_name),
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data,
    }).encode()
    req = urllib.request.Request(event["ResponseURL"], data=body, headers={"content-type": ""}, method="PUT")
    urllib.request.urlopen(req)


def handler(event, context):
    name = event["ResourceProperties"]["ParameterName"]
    try:
        ssm = boto3.client("ssm")
        if event["RequestType"] == "Delete":
            try:
                ssm.delete_parameter(Name=name)
            except ssm.exceptions.ParameterNotFound:
                pass
        elif event["RequestType"] == "Create":
            secret = base64.b64encode(secrets.token_bytes(32)).decode()
            try:
                ssm.put_parameter(Name=name, Value=secret, Type="SecureString", Overwrite=False)
            except ssm.exceptions.ParameterAlreadyExists:
                pass
        cfn_send(event, context, "SUCCESS", {"ParameterName": name})
    except Exception as e:
        print(f"gossip secret {event['RequestType']} for {name} failed (reason=ssm_error): {type(e).__name__}: {e}")
        cfn_send(event, context, "FAILED", {"Error": str(e)})
