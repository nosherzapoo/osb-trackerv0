# osb-ops-api install runbook (VPS)

One-time bootstrap. Re-run individual steps after a code change as needed.

## 1. Install Python deps into the existing venv

```bash
ssh root@104.238.164.81
cd /srv/osb-trackerv0
.venv/bin/pip install -r ops_api/requirements.txt
```

## 2. Generate auth secrets and add them to `/srv/osb-trackerv0/.env`

```bash
# Pick a strong password and hash it
.venv/bin/python -m ops_api.hash_password 'your-strong-password' > /tmp/hash
cat /tmp/hash   # copy the bcrypt string

# Generate a JWT secret
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Append to `/srv/osb-trackerv0/.env`:

```
ADMIN_USERNAME=nosher
ADMIN_PASSWORD_HASH=<paste bcrypt string>
JWT_SECRET=<paste token>
JWT_TTL_HOURS=24
OPS_CORS_ORIGINS=https://app.osbdata.com,http://localhost:5173
```

## 3. Install the systemd unit

```bash
cp /srv/osb-trackerv0/scripts/systemd/osb-ops-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now osb-ops-api.service
systemctl status osb-ops-api.service
journalctl -u osb-ops-api.service -n 30 --no-pager
```

Sanity-check the service is up:

```bash
curl -s http://127.0.0.1:8001/auth/me
# expects: {"detail":"missing bearer token"}
```

## 4. Wire up nginx

Open `/etc/nginx/sites-available/api.osbdata.com` (or wherever the
`api.osbdata.com` server block lives) and paste the contents of
`scripts/nginx/api-osbdata-com-ops.conf` inside the `server { ... }` for
that domain.

```bash
nginx -t && systemctl reload nginx
```

Smoke-test through nginx:

```bash
curl -s https://api.osbdata.com/ops/auth/me
# expects: {"detail":"missing bearer token"}
```

## 5. Get an access token

```bash
curl -s -X POST https://api.osbdata.com/ops/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"nosher","password":"your-strong-password"}'
```

Copy the `access_token` and try a real endpoint:

```bash
TOKEN=...
curl -s https://api.osbdata.com/ops/system/health \
  -H "Authorization: Bearer $TOKEN" | jq
curl -s https://api.osbdata.com/ops/states \
  -H "Authorization: Bearer $TOKEN" | jq '.states[0:2]'
```

## Updating after a code change

```bash
ssh root@104.238.164.81 'cd /srv/osb-trackerv0 && git pull --rebase --autostash && systemctl restart osb-ops-api'
```
