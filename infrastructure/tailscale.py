"""Tailscale Configuration."""

import json

import pulumi
import pulumi_tailscale as tailscale

# acl_policy = {
#     "groups": {
#         "group:ops": ["your-email@example.com"],
#     },
#     "tagOwners": {
#         "tag:server": ["group:ops"],
#         "tag:admin": ["group:ops"],
#     },
#     "acls": [
#         {
#             "action": "accept",
#             "src": ["tag:admin"],
#             "dst": ["*:*"],
#         },
#         {
#             "action": "accept",
#             "src": ["tag:server"],
#             "dst": ["tag:server:80", "tag:server:443"],
#         },
#     ],
# }

# tailnet_acl = tailscale.Acl("main-acl", acl=json.dumps(acl_policy))
