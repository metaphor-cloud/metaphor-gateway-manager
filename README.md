# metaphor-gateway-manager

The Lambda functions a Metaphor Gateway deployment needs, as one AWS Serverless
Application Repository app to nest in your own CloudFormation. Every function
runs from the same code package (`src/`), one module per entrypoint:

| Module | What it does | Mode |
| --- | --- | --- |
| `eip_manager` | Attaches a public ENI and Elastic IP to each gateway on launch; migrates or releases it on terminate | gwlb |
| `eip_cleanup` | Custom resource: on stack delete, releases tagged EIPs and deletes managed ENIs left behind | gwlb |
| `nat_cutover` | Moves each NAT-only group's persistent egress ENI onto its new gateway inside the launch lifecycle | nat-only |
| `gossip_secret` | Custom resource: generates the gossip HMAC key as an SSM SecureString | both |
| `ami_updater` | Scheduled: starts an instance refresh when the gateways are behind the latest published AMI | both |

Metaphor Gateway's AWS Marketplace templates ship the same modules inline,
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
      SemanticVersion: 0.1.1
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

Make the gateway Auto Scaling group depend on `GatewayManager` and `EipCleanup`,
so its first lifecycle hooks reach a function that exists and the cleanup sweep
runs after the group is gone on delete.

NAT-only, one Auto Scaling group of one per egress ENI:

```yaml
GatewayManager:
  Type: AWS::Serverless::Application
  Properties:
    Location:
      ApplicationId: arn:aws:serverlessrepo:us-east-1:189292791360:applications/metaphor-gateway-manager
      SemanticVersion: 0.1.1
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
