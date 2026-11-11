# Vendored Scalar API reference

`standalone.js` is the published browser bundle of `@scalar/api-reference` **1.78.0**,
copied here so `/docs` loads nothing from another origin at run time. It is MIT licensed,
like this service.

- Pinned version: `@scalar/api-reference@1.78.0`
- Downloaded from: `https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.78.0/dist/browser/standalone.js`
- SHA-256: `954b54c4e9e38abe6db9bbd504acd76aae3be473117a1b63a87b34a0298c629b`

The page that loads it is `../docs.html`, served by `GET /docs` in `../../app.py`. The
configuration there turns off Scalar's request proxy, its web fonts, its telemetry and its
"open in client" button, so the reference works with no network at all.

To move to a newer version: download the same URL with the new version number, replace
`standalone.js`, and update the version and the SHA-256 above. Do not edit the bundle.
`tests/test_api_reference.py` checks the digest, and its browser test loads the page with
every other origin blocked and runs "try it".
