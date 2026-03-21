# To Do List

## Functionality

- [x] Trigger Postgres backup on `pulumi down`
- [x] Restore latest Postgres backup on `pulumi up`
- [x] Write integration test for Postgres S3 backup

## Automation

- [x] Integrate SkyPilot MLflow pipeline
- [ ] Set SkyPilot config variable `admin policy` via Pulumi
- [ ] Make SkyPilot pick up a changed admin policy on `pulumi up`
- [ ] Handle failed CNPG cluster creation gracefully, by avoiding orphaned CRs

## Cleanup

- [ ] Make SkyPilot admin policy modular
- [ ] Restructure and format SkyPilot-MLflow-integration test
- [x] Normalize variable naming conventions across the codebase
