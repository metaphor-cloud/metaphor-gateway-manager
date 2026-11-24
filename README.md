# metaphor-gateway-manager

The Lambda functions a Network by Metaphor deployment needs, as one AWS Serverless
Application Repository app to nest in your own CloudFormation. Every function
runs from the same code package (`src/`), one module per entrypoint:

| Module | What it does | Mode |
| --- | --- | --- |
| `eip_manager` | Attaches a public ENI and Elastic IP to each gateway on launch; migrates or releases it on terminate | gwlb |
| `eip_cleanup` | Custom resource: on stack delete, releases tagged EIPs and deletes managed ENIs left behind | gwlb |
| `nat_cutover` | Moves each NAT-only group's persistent egress ENI onto its new gateway inside the launch lifecycle | nat-only |
| `gossip_secret` | Custom resource: generates the gossip HMAC key as an SSM SecureString | both |
| `network_details` | Custom resource: keeps a VPC's or subnets' IPv4 CIDRs in a managed prefix list for security group rules | both |
| `ami_updater` | Scheduled: starts an instance refresh when the gateways are behind the latest (or stable) AMI; with `Channel=stable` also the `Custom::StableAmi` custom resource | both |

Network by Metaphor's AWS Marketplace templates ship the same modules inline,
because Marketplace does not accept templates that deploy SAR resources.

## Using it

Pass your own stack name as `GatewayStackName`. The functions name themselves
`<GatewayStackName>-<function>`, tag what they create with
`metaphor:stack-name=<GatewayStackName>`, and the gossip key must live at
`/flow/<GatewayStackName>/gossip-hmac`.

gwlb:

```yaml
GatewayManager:
  Type: AWS::Serverless::Application
  Properties:
    Location:
      ApplicationId: arn:aws:serverlessrepo:us-east-1:189292791360:applications/metaphor-gateway-manager
      SemanticVersion: 0.2.0
    Parameters:
      Mode: gwlb
      GatewayStackName: !Ref AWS::StackName
      AsgName: !Sub '${AWS::StackName}-asg'
      PublicSubnets: !Join [',', [!Ref PublicSubnetA, !Ref PublicSubnetB]]
      PublicSecurityGroup: !Ref PublicEniSecurityGroup
      EipAllocationIds: !Join [',', [!Ref EipA, !Ref EipB]]
      EipsPerAz: '1'
      TargetGroupArn: !Ref GatewayTargetGroup

GossipHmacSecret:
  Type: Custom::GossipSecret
  Properties:
    ServiceToken: !GetAtt GatewayManager.Outputs.GossipSecretServiceToken
    ParameterName: !Sub '/flow/${AWS::StackName}/gossip-hmac'

EipCleanup:
  Type: Custom::EipCleanup
  Properties:
    ServiceToken: !GetAtt GatewayManager.Outputs.EipCleanupServiceToken
    StackName: !Ref AWS::StackName
```

Security group sources from the network's real ranges, not a typed-in CIDR:

```yaml
VpcPrefixList:
  Type: Custom::NetworkDetails
  Properties:
    ServiceToken: !GetAtt GatewayManager.Outputs.NetworkDetailsServiceToken
    Name: !Sub '${AWS::StackName}-vpc'
    VpcId: !Ref VpcId

GwlbSubnetPrefixList:
  Type: Custom::NetworkDetails
  Properties:
    ServiceToken: !GetAtt GatewayManager.Outputs.NetworkDetailsServiceToken
    Name: !Sub '${AWS::StackName}-gwlb-subnets'
    SubnetIds: !Join [',', [!Ref ApplianceSubnetA, !Ref ApplianceSubnetB]]

# then, in a security group rule:
#   SourcePrefixListId: !GetAtt VpcPrefixList.PrefixListId
```

A prefix list counts against a security group's rule quota at its maximum
size: a VPC list is created with room for five blocks, a subnet list with one
entry per subnet. An update rewrites the same list, so the security groups
that reference it are never replaced. Re-run a stack update after adding a
CIDR block to the VPC to pick it up.

Make the gateway Auto Scaling group depend on `GatewayManager` and `EipCleanup`,
so its first lifecycle hooks reach a function that exists and the cleanup sweep
runs after the group is gone on delete.

`EipsPerAz` 2 or more is a strict pool: the gateways only ever egress from the
EIPs in `EipAllocationIds`. A launch that finds every slot in its AZ held waits
for one to free (an outgoing gateway returns its slot in its terminate hook),
heartbeating the launch hook, and is abandoned if none frees within the
function's 15 minutes. Size the pool to the gateways you run per AZ; when an
AZ's pool is full the AMI updater refreshes terminate-first, one gateway at a
time, instead of launching the replacement first.

NAT-only, one Auto Scaling group of one per egress ENI:

```yaml
GatewayManager:
  Type: AWS::Serverless::Application
  Properties:
    Location:
      ApplicationId: arn:aws:serverlessrepo:us-east-1:189292791360:applications/metaphor-gateway-manager
      SemanticVersion: 0.2.0
    Parameters:
      Mode: nat-only
      GatewayStackName: !Ref AWS::StackName
      NatAsgNames: !Sub '${AWS::StackName}-nat-a,${AWS::StackName}-nat-b'
      EgressEniIds: !Join [',', [!Ref EgressEniA, !Ref EgressEniB]]
```

Each NAT-only group needs a launch hook with `HeartbeatTimeout: 900` and
`DefaultResult: ABANDON`, and its launch template must tag instances and network
interfaces `metaphor:stack-name=<GatewayStackName>`.

Set `AutoUpdateEnabled=false` when the launch template pins an AMI version.

### Following stable instead of latest

`Channel=stable` follows the newest version that stayed the latest published
version for `StableMinAgeDays` (default 2) without a newer one replacing it, so
production runs only a release that has already been the newest for that long.
The app writes that AMI to `/flow/<GatewayStackName>/stable-ami`. Point the
launch template at it and create the parameter before the groups launch:

```yaml
LaunchTemplateData:
  ImageId: !Sub resolve:ssm:/flow/${AWS::StackName}/stable-ami

StableAmi:
  Type: Custom::StableAmi
  Properties:
    ServiceToken: !GetAtt GatewayManager.Outputs.StableAmiServiceToken

GatewayGroup:
  Type: AWS::AutoScaling::AutoScalingGroup
  DependsOn: StableAmi
```

With `AutoUpdateEnabled=true` the scheduled check moves the parameter when a
newer version qualifies and rolls the groups onto it. Without it, stable is
fixed when the stack is created; invoking the function moves it and rolls the
groups.

## Developing

```
pip install -r requirements-dev.txt
python -m pytest -q tests
sam validate --lint
```

Each module must stay a single self-contained file: the Marketplace templates put
each one in a `ZipFile`, which holds exactly one.

## Releasing

Bump `SemanticVersion` in `template.yaml` and merge to `main`. The pipeline in
`cfn/pipeline.yml` runs the tests, publishes the version to the Serverless
Application Repository and makes it publicly deployable. Publishing a version
that already exists is a no-op.
