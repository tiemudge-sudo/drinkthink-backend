# DrinkThink Master Canonical Correction v2

Authoritative source generation: the contents of `DrinkThink_db.zip` supplied by the user.

This revision resolves the four known bridge collisions from `canonical-reset-v3` by explicit canonical document identity. It does **not** introduce name matching.

Explicit repairs:
- master 1 -> `ckt_fa85b1610cf1c9037a6a`; clear corrupt nested bridge from `ckt_2c601e519114bb8da094`
- master 2 -> `ckt_322c1edca869513e2995`; clear corrupt nested bridge from `ckt_54b0537b291c8b5bcc79`
- master 5 -> `ckt_09b83c71abc3b22b1683`; clear corrupt nested bridge from `ckt_4b26cfb5619351939abb`
- master 7 -> `ckt_18c8ee0de51e30ac38bf`; clear corrupt nested bridge from `ckt_e9f991046ec2717349a4`

The unrelated cocktails are retained. Only their erroneous nested `migration.legacy_drink_id` value is removed when it still equals the corrupted value.

## Dry run first

```bash
python tools/master_canonical_correction_v1.py --report master-canonical-correction-v2-report.json
```

Expected gate before apply:
- 12,256 / 12,256 master cocktails bridged or creatable
- 4 explicit bridge repairs
- 562 create-from-master rows
- 46,577 / 46,577 recipe relationships
- 0 recipe failures
- `apply_gate_passed: true`

Do not apply unless those checks pass.

## Apply (only after dry-run review)

```bash
python tools/master_canonical_correction_v1.py --apply --confirm-apply MASTER_CANONICAL_CORRECTION_V2 --report master-canonical-correction-v2-apply-report.json
```
