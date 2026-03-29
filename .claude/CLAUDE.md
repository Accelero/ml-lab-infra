# Claude Rules

## General

- Don't use em dashes or en dashes as sentence separators in prose or comments. Use a period or restructure the sentence instead.
- Run ruff check at the end of your editing loop.
- Run ruff check with `--select ALL`, unless told otherwise.
- To run kubectl commands, use the exported kubeconfig exported from Pulumi via `pulumi stack output hub_kubeconfig --show-secrets > /tmp/kubeconfig.yaml`
- Try to resolve linter warnings by actually fixing them and not by disabling the warning.
