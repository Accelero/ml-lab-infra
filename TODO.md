# To Do List

## Functionality

- [ ] Trigger Postgres backup on `pulumi down`
- [ ] Restore latest Postgres backup on `pulumi up`

## Automation

- [x] Integrate SkyPilot MLflow pipeline
- [ ] Set SkyPilot config variable `admin policy` via Pulumi
- [ ] Make SkyPilot pick up a changed admin policy on `pulumi up`

## Cleanup:

- [ ] Make SkyPilot admin policy modular
- [ ] Restructure and format SkyPilot-MLflow-integration test
- [ ] Normalise variable naming conventions across the codebase
- [ ] Write integration test for Postgres S3 backup
