"""Custom resource: sweep orphan EIPs and ENIs on stack delete.

The EIP manager tags the public ENIs and transient EIPs it creates with
metaphor:stack-name=<StackName>. In the normal terminate path they are released,
but if a refresh aborts mid-flight or the ASG is force-deleted they leak, and
security group deletion then fails on a dependent object. On Delete this
releases every EIP and deletes every managed ENI carrying the tag. EIPs the
caller allocated are only ever associated by the manager, never tagged, so they
are never touched.

Always reports SUCCESS: a failed sweep must not block the stack delete, and
every failure is logged.
"""

import json
import time
import urllib.request

import boto3


def cfn_send(event, context, status, data=None):
    body = json.dumps({
        "Status": status,
        "Reason": "See " + context.log_stream_name,
        "PhysicalResourceId": event.get("PhysicalResourceId", context.log_stream_name),
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {},
    }).encode()
    req = urllib.request.Request(event["ResponseURL"], data=body, headers={"content-type": ""}, method="PUT")
    urllib.request.urlopen(req)


def release_orphan_eips(ec2, stack):
    addrs = ec2.describe_addresses(Filters=[
        {"Name": "tag:metaphor:stack-name", "Values": [stack]},
    ])["Addresses"]
    for a in addrs:
        aid = a.get("AssociationId")
        alid = a.get("AllocationId")
        if aid:
            try:
                ec2.disassociate_address(AssociationId=aid)
                print(f"Disassociated {aid}")
            except Exception as e:
                print(f"disassociate {aid} failed: {e}")
        if alid:
            try:
                ec2.release_address(AllocationId=alid)
                print(f"Released orphan EIP {alid} (stack={stack})")
            except Exception as e:
                print(f"release {alid} failed: {e}")


def delete_orphan_enis(ec2, stack, sleep=time.sleep):
    # Managed ENIs (DeviceIndex=1 on each NAT instance, created by the EIP
    # manager). When the ASG terminates an instance, AWS detaches the ENI but
    # does not delete it (no DeleteOnTermination). The manager's terminate hook
    # does the delete, but if it errors or times out the ENI lingers.
    enis = ec2.describe_network_interfaces(Filters=[
        {"Name": "tag:metaphor:managed", "Values": ["true"]},
        {"Name": "tag:metaphor:stack-name", "Values": [stack]},
    ])["NetworkInterfaces"]
    for n in enis:
        ni = n["NetworkInterfaceId"]
        att = n.get("Attachment")
        if att and att.get("AttachmentId"):
            try:
                ec2.detach_network_interface(AttachmentId=att["AttachmentId"], Force=True)
                print(f"Detached {ni}")
            except Exception as e:
                print(f"detach {ni} failed: {e}")
            # Wait up to 60s for status=available.
            for _ in range(30):
                try:
                    s = ec2.describe_network_interfaces(NetworkInterfaceIds=[ni])["NetworkInterfaces"][0]
                    if s["Status"] == "available":
                        break
                except Exception:
                    break
                sleep(2)
        try:
            ec2.delete_network_interface(NetworkInterfaceId=ni)
            print(f"Deleted orphan ENI {ni} (stack={stack})")
        except Exception as e:
            print(f"delete {ni} failed: {e}")


def handler(event, context):
    try:
        if event["RequestType"] == "Delete":
            stack = event["ResourceProperties"]["StackName"]
            ec2 = boto3.client("ec2")
            release_orphan_eips(ec2, stack)
            delete_orphan_enis(ec2, stack)
        cfn_send(event, context, "SUCCESS")
    except Exception as e:
        print(f"EipCleanup handler error (reason=sweep_failed): {type(e).__name__}: {e}")
        cfn_send(event, context, "SUCCESS")
