# Vendor Management

Owner: CISO. Reviewed annually and when a vendor is added.

## Principles

- **One inference path.** Every model call goes through Reg42 Infer to Amazon Bedrock;
  no other inference provider exists in production (`tests/test_never_list.py::test_only_infer_provider_in_prod`).
  Model ids are frozen per release in the model manifest.
- **No customer data leaves the account.** Agnostic mode holds no organisation data (I5);
  instance mode runs in the client's own account (item 18).
- **Verify, don't trust, artefacts.** Dependencies pinned and hash-checked; releases signed.

## Register

| Vendor | Used for | Data it sees | Assurance | Fallback |
|---|---|---|---|---|
| Amazon Web Services | compute, storage, Aurora, Cognito, Bedrock (via Infer), CloudWatch, AMP/AMG | all service data, in Reg42's account | AWS SOC 1/2/3, ISO 27001 (AWS Artifact) | cross-region DR (`BUSINESS_CONTINUITY.md`) |
| Reg42 Infer | model routing to Bedrock | prompts derived from public/licensed regulatory text; never member data | internal; same account | none — derivation pauses, serving continues |
| GitHub | source, CI, public `clhear` repo, releases | code, public artefacts | GitHub SOC 2 | mirror + signed artefacts allow re-hosting |
| Discourse (hosted) | community forum | contributor emails, posts | Discourse SOC 2 | export; forum is not in the record |
| esm.sh | CDN for React/htm and the d3 modules (`d3-force`, `d3-zoom`, `d3-selection`, `d3-drag`, all `@3.0.0`) behind the no-build pages and the graph canvas | none (client-side fetch) | public CDN | pin versions; vendor the files if the CDN fails |
| Upptime (GitHub Actions) | independent uptime probe of `/status.json` | none | runs in our GitHub org | `/status.json` remains authoritative |
| Regulatory publishers | source texts | none (we fetch) | rights basis recorded per source (`app/clhear/l1/rights.py`) | rights-aware serving; derived facts only when text is withheld |

## Onboarding a vendor

1. Purpose, data classification touched (`DATA_CLASSIFICATION.md`), region.
2. Assurance evidence (SOC 2 / ISO 27001) or a documented reason it is not needed.
3. Fallback if the vendor disappears for a week.
4. Entry in this register; PR reviewed by the CISO.
