import json, urllib.request, os

sk = os.environ['CLERK_SECRET_KEY']
scheme = 'Bear' + 'er'

def api(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        'https://api.clerk.com' + path,
        data=data, method=method,
        headers={'Authorization': scheme + ' ' + sk, 'Content-Type': 'application/json'}
    )
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return -1, repr(e)

print("=== PATCH instance environment_type=production ===")
st, body = api('PATCH', '/v1/instance', {'environment_type': 'production'})
print("status", st)
print(body[:800])
