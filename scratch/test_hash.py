import hashlib

payload = '{"method":"devSta.get","params":{"module":"user_list","noParse":true,"async":null,"remoteIp":false,"data":{"devType":"all","dataType":"timely"},"device":"pc"}}'
token = '9fc3ed4fdf2096156e5c00c294492231'

target_accept = '81459ecae89d3194507c563c0855af07'
target_accepts = '2e040c671559093241b774f06508c065'

def md5(s):
    return hashlib.md5(s.encode('utf-8')).hexdigest()

combinations = [
    payload,
    token,
    payload + token,
    token + payload,
    payload + token + 'ruijie',
    md5(payload) + token,
    token + md5(payload)
]

print("Target content-accept:", target_accept)
for c in combinations:
    h = md5(c)
    if h == target_accept:
        print("MATCH content-accept! Formula:", c)
    if h == target_accepts:
        print("MATCH contents-accept! Formula:", c)
        
print("Done.")
