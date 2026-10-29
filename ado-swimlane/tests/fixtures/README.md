`expanded-3384-omitted-relations.json` is a sanitized shape projection of the
fresh, parent-owned one-item diagnostic observed at 2026-10-29T15:05:58Z
(provider Date 15:06:05Z). Its request was POST workitemsbatch with
`ids: [3384]`, `$expand: Relations`, and `errorPolicy: omit`. The complete
HTTP 200 response had count 1, item ID 3384, revision 2, and no `relations`
property. Original response SHA256:
`392b1b59a54928ac1e6131051e7f12b6048ca4c8a23fbbbf1f994cefeb3dbb3a`.

Identity, descriptions, history, and other unused fields were removed; title,
iteration, and URL are synthetic. Unused top-level objects are empty. This
fixture is not the original 7096-byte response and does not reconstruct the
14:58:55Z historical failure or prove that omitted relations means zero.

The current-template replay in `test_relation_fallback.py` is synthetic for every
other record. It exercises the entire board through HTTP-response decoding,
normalization, rendering, and rejection. It is not a retained full provider
collection. Missing whole records and omitted relation evidence remain unknown.

`3384-empty-dependency-links.json` projects the separate parent-owned one-source
MustContain query observed at 2026-10-29T15:10:44Z, with `asOf`
2026-10-29T15:10:52.08Z and a complete HTTP 200 empty `workItemRelations` array.
Original response SHA256:
`cb39bea0f68e7a428118cd59b8f4434f0c2a630188b638052640c19e78ade6a7`.
Only field URLs are sanitized. This establishes zero dependency edges for that
source at that observation; it does not establish generic omitted-field semantics.
