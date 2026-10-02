#!/usr/bin/env python3
import os

import aws_cdk as cdk

from infra.github_oidc_stack import GithubOidcStack
from infra.shuto_stack import ShutoStack

app = cdk.App()
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "ap-northeast-1"),
)
ShutoStack(
    app,
    "ShutoStack",
    env=env,
    description="Shuto: weekly photo batches -> Bedrock metadata -> Shutterstock",
)
GithubOidcStack(
    app,
    "ShutoGithubOidc",
    env=env,
    description="Shuto: GitHub Actions OIDC deploy role (deploy manually, once)",
)
cdk.Tags.of(app).add("project", "shuto")
app.synth()
