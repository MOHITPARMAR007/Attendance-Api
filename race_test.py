import json
import threading
import urllib.request
import urllib.error
from collections import Counter

URL = "http://127.0.0.1:8000/attendance/punch-in"
BODY = json.dumps({"emp_code": "EMP0002", "punched_at": 1784520900000}).encode()
N = 10
barrier = threading.Barrier(N)  # sab threads ek saath chhootenge
results = []


def hit():
    req = urllib.request.Request(URL, data=BODY, headers={"Content-Type": "application/json"})
    barrier.wait()
    try:
        results.append(urllib.request.urlopen(req).status)
    except urllib.error.HTTPError as e:
        results.append(e.code)


threads = [threading.Thread(target=hit) for _ in range(N)]
[t.start() for t in threads]
[t.join() for t in threads]
print(Counter(results))