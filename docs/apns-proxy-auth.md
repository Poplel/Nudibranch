# APNS Proxy Authentication — Design

**Status:** Design only. No code in this document is implemented. Lands alongside the
iOS app (see `docs/ios-app.md`; iOS app itself is deferred/user-built per the project plan).

**Goal (user's words):** "fully prevent this from being exploitable and ensure it can
only be used by Nudibranch and works out of the box for any new users."

**Confirmed deployment model:** the project owner has an Apple Developer account and will
**host the central APNS proxy** and publish the single iOS app. Self-hosters do **not** need
their own Apple account — their Nudibranch servers relay notifications through the hosted
proxy. This is decisive: the proxy is exposed to **arbitrary, untrusted self-hosted servers**,
so the server cannot be the trust anchor and App Attest (§4) is load-bearing, not optional.
One published app means App Attest validates the *app* (not each server), so every
self-hoster's notifications work out of the box. "Self-hosters use direct mode with their own
`.p8`" is **not** the intended path.

---

## 1. The current design and why it is not enough

`backend/nudibranch/services/notifications.py` proxy mode authenticates with a single
shared secret:

```python
# notifications.py:31
_PROXY_CLIENT_SECRET = "nb-proxy-v1-placeholder-replace-before-deploying-proxy"
```

Every push is HMAC-SHA256 signed over `timestamp:nonce:instance_id:apns_token:title:body`
(`_proxy_signature`, line 44) and POSTed to `{proxy}/push` with the signature attached.

**This cannot meet the stated goal as written.** The secret lives in identical,
open-source, self-hosted server code. The moment it is in the public repo, anyone who
reads GitHub has it and can sign requests indistinguishable from a "legitimate Nudibranch
instance." HMAC over a *public* secret authenticates nobody.

The deeper reason is structural, and worth stating plainly so we don't keep chasing it:

> You cannot have a credential that is both (a) embedded in public, identical,
> self-hosted **server** code and (b) actually secret. A self-hosted server has no
> hardware root of trust to vouch for its identity. An **iOS app** does
> (Apple App Attest). So "only Nudibranch can use it" must be anchored in the app,
> not the server.

---

## 2. What the reference implementations do

Two open-source push gateways bracket the design space:

### Matrix / Sygnal — *don't authenticate the gateway at all*
The Matrix push gateway `/notify` endpoint requires **no authentication**
("Requires authentication: No" in the spec). The entire model rests on one fact: a push
key *is* an APNS device token, and APNS will only ever deliver to a token registered to
*their* app's bundle ID. Abuse is bounded by "you can only reach devices whose tokens you
already possess," plus a rejection-list feedback loop. They concluded server→gateway auth
wasn't worth it because **APNS itself is the real authorization boundary.**

### OpenClaw — *anchor trust in the device via App Attest*
The hardened model, and the closest analog to our goal. At registration the iOS app
presents **Apple App Attest** + a **StoreKit app-transaction JWS**; the relay validates
bundle ID + App Attest + "official Apple distribution proof." This is explicitly what
"blocks local Xcode/dev builds." The relay then mints a **registration-scoped send grant
delegated to a specific gateway identity**, owns the APNS credentials *and the raw device
token*, and verifies the grant + a gateway signature on every push. A stolen handle is
useless to a different gateway.

**Takeaway:** the robust answer moves the trust anchor from the *server* (which can prove
nothing about itself) to the *device* (which Apple hardware vouches for).

---

## 3. Threat model — what abuse actually buys an attacker

Important for not over-engineering:

- APNS only delivers to tokens registered to **our** bundle ID. An attacker who fully
  defeats proxy auth still **cannot push to arbitrary phones** — only to devices running
  the genuine Nudibranch app.
- To reach a specific device, the attacker needs that device's **APNS token**, which is
  registered to a Nudibranch instance via `POST /notifications/devices`. Tokens are not
  public.
- With the §4 per-pairing grant, **even knowing the token is not enough** — a server can
  only push to devices that authorized *it* (granted to its `instance_id`). So realistic
  abuse collapses to: a server the user *did* pair sends unwanted pushes (already in the
  user's trust boundary; revoke = unpair), plus generic cost/reputation load on the proxy.

The worst case is **not** "push to anyone," and not even "push to anyone whose token you
know" — it is "a server you deliberately added behaves badly." The design should be
proportionate to that.

---

## 4. Target design — App Attest device send-grants

Adopt the OpenClaw model, adapted to Nudibranch's topology
(self-hosted server → project proxy → APNS → device).

### Roles
- **Device / iOS app** — the only component with a hardware-backed identity (App Attest).
- **Nudibranch server** (self-hosted; "gateway" in OpenClaw terms) — relays pushes; holds
  no proxy secret and ideally never sees the raw APNS token.
- **Proxy** (project-run) — holds the Apple `.p8` credentials and the raw APNS tokens;
  mints and verifies send-grants.

### Flow

> **The grant is scoped to a `(device, server)` pairing — not just a device token.** App
> Attest only proves the *app* is genuine; it does not say *which server* may push to a
> device. If a grant were device-only, any server that learned the token could push. The
> per-pairing scope is what makes "only servers I added can notify me" actually true.

1. **Attested registration (app → proxy).** On a real device, the app performs App Attest
   with the proxy and includes its StoreKit app-transaction JWS. The proxy validates
   bundle ID + App Attest + production-distribution proof, then stores the raw APNS token
   bound to this attested device identity. No server is involved yet.
2. **Pairing (app authorizes a server, per server).** When the user adds a server in the
   app (URL + login), the server hands the app its **stable server identity** —
   Nudibranch already mints one: `instance_id` via `_get_or_create_instance_id`
   (`notifications.py`). The app tells the **proxy** "authorize `instance_id=X` to push to
   *my* device," and the proxy records a **send-grant scoped to `(this device token, X)`**,
   short-lived/renewable, revocable, and returns it for the app to hand to server X (over
   the existing authenticated `POST /notifications/devices`, extended to carry the grant).
3. **Push (server → proxy).** Server X presents its grant. The proxy verifies the grant is
   valid for *(that exact device, X)* and not revoked, then delivers — and **tags the push
   with X's `instance_id` and the friendly label the user saved at pairing**, so the app
   knows the source. The app keeps its own paired-server list and ignores pushes from a
   server not on it (defense in depth).

**Stolen-grant resistance (recommended, OpenClaw-style):** give each server a keypair tied
to its `instance_id`, registered with the proxy at pairing; the server **signs** every push
and the proxy verifies against that key. A leaked grant is then useless without the server's
private key.

**Multi-server falls out for free:** one grant per pairing, each notification attributed by
server, and unpairing a server just revokes that one grant — no effect on the others.

### Why this meets all three goals genuinely
- **Out of the box** — no Apple Developer account, no manual secret provisioning. Install
  the app, log into your server, done.
- **Only Nudibranch** — App Attest cannot be forged off genuine Apple hardware + a genuine
  App-Store build; dev/sideloaded builds are rejected at registration. The anchor is real.
- **Not exploitable** — a leaked grant's blast radius is "spam that one device," and it is
  rate-limitable, revocable, and short-lived. The proxy can hold the raw token so a
  compromised self-hosted server can't exfiltrate tokens or push cross-device.

### Dependency / sequencing
This requires the iOS app, which does not exist yet. App Attest is a client capability;
there is nothing to build server-side in isolation that can be exercised without it.
**Implement this phase together with the iOS app**, not before.

---

## 5. Interim hardening (only if proxy ships before the app)

If proxy mode must go live before App Attest exists, stop treating the secret as security
and harden the proxy's *actual* boundary instead. None of this is a substitute for §4 —
it just lowers the floor.

- **Get the secret out of the public repo.** Inject it into the hosted proxy + official
  deployment image only (env var / build-time). Removes the published-secret footgun. (Direct
  mode / `_deliver_direct` remains as a fallback for anyone who wants to run their own proxy,
  but per the confirmed model the default self-hoster path is relaying through the hosted
  proxy — not bringing their own Apple account.)
- **Real replay protection.** The proxy must **persist `nonce`** and reject reuse, and
  reject `timestamp` outside a tight window (a few minutes). Today the nonce is decorative
  unless the proxy stores it.
- **TOFU token-binding.** On first sight, the proxy binds a device token to the
  `instance_id` that registered it; pushes to that token from any other `instance_id` are
  rejected. Stops cross-instance hijacking even without App Attest.
- **Per-instance and per-token rate limits** on the proxy (the client already handles a
  `429`; the proxy must actually enforce one).

Because APNS already constrains delivery to our bundle ID and an attacker needs a victim's
token, this interim posture covers the realistic threat (§3) without pretending the shared
secret is meaningful.

---

## 6. Decision

- **Long term:** App Attest device send-grants (§4), shipped with the iOS app.
- **Short term (only if needed):** §5 hardening; never rely on the in-repo shared secret.
- **Do not** invest further in making the hardcoded `_PROXY_CLIENT_SECRET` "more secret" —
  it is structurally unfixable in open source.

## Sources
- Matrix Push Gateway API spec — https://spec.matrix.org/unstable/push-gateway-api/
- Sygnal (reference Matrix push gateway) — https://github.com/matrix-org/sygnal
- OpenClaw iOS / APNS relay docs — https://docs.openclaw.ai/platforms/ios
