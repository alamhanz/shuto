#!/usr/bin/env python3
import os

import aws_cdk as cdk

from infra.shuto_stack import ShutoStack

app = cdk.App()
ShutoStack(
    app,
    "ShutoStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "ap-northeast-1"),
    ),
    description="Shuto: weekly photo batches -> Bedrock metadata -> Shutterstock",
)
cdk.Tags.of(app).add("project", "shuto")
app.synth()
