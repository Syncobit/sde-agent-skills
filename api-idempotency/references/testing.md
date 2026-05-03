# Testing — Five Canonical Tests for an Idempotency Layer

If you don't run these, you don't know whether your idempotency layer works. The bugs here only show up under retry storms or production failure modes — they will not surface in normal integration testing.

## Test 1 — Replay correctness

**What it checks:** the same request twice produces the same response, and only one execution.

```python
async def test_replay_returns_identical_response():
    key = str(uuid.uuid4())
    body = {"amount": 100, "currency": "USD", "customer": "cus_1"}

    r1 = await client.post("/v1/charges",
                            headers={"Idempotency-Key": key},
                            json=body)
    r2 = await client.post("/v1/charges",
                            headers={"Idempotency-Key": key},
                            json=body)

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json() == r2.json()              # identical body
    assert r2.headers.get("Idempotent-Replayed") == "true"
    assert count_charges_in_db(customer="cus_1") == 1   # exactly one side effect
```

The side-effect assertion is the one that catches real bugs. A broken idempotency layer can return matching responses while still executing twice.

## Test 2 — Concurrency

**What it checks:** N parallel requests with the same key produce exactly one execution.

```python
async def test_concurrent_requests_execute_once():
    key = str(uuid.uuid4())
    body = {"amount": 100, "currency": "USD", "customer": "cus_2"}

    tasks = [
        client.post("/v1/charges",
                    headers={"Idempotency-Key": key},
                    json=body)
        for _ in range(20)
    ]
    responses = await asyncio.gather(*tasks)

    assert count_charges_in_db(customer="cus_2") == 1

    success = [r for r in responses if r.status_code == 201]
    in_flight = [r for r in responses if r.status_code == 409]

    # Exactly one must have succeeded; the rest are 409 in-flight or 201 replays.
    assert len(success) >= 1
    assert len(success) + len(in_flight) == 20

    # All successful responses must be byte-identical (one was the real one, others were replays).
    bodies = {r.text for r in success}
    assert len(bodies) == 1
```

Run this test against a real Redis or Postgres instance, not a mock. The bug being caught is a race in the atomic-insert step; mocks won't reproduce it.

## Test 3 — Fingerprint mismatch

**What it checks:** reusing a key with a different body is rejected, and does not execute the second body.

```python
async def test_key_reuse_with_different_body_is_rejected():
    key = str(uuid.uuid4())

    r1 = await client.post("/v1/charges",
                            headers={"Idempotency-Key": key},
                            json={"amount": 100, "currency": "USD"})
    assert r1.status_code == 201

    r2 = await client.post("/v1/charges",
                            headers={"Idempotency-Key": key},
                            json={"amount": 999, "currency": "USD"})

    assert r2.status_code == 422
    assert r2.json()["type"].endswith("/idempotency-key-mismatch")
    assert count_charges_in_db_with_amount(999) == 0   # second body did NOT execute
```

The "did not execute" assertion is critical. Some buggy implementations return 422 *after* executing the request — the worst possible outcome.

## Test 4 — TTL expiry behavior

**What it checks:** after the TTL elapses, the same key is treated as a new request.

```python
async def test_expired_key_is_treated_as_new():
    key = str(uuid.uuid4())
    body = {"amount": 100, "currency": "USD", "customer": "cus_4"}

    r1 = await client.post("/v1/charges",
                            headers={"Idempotency-Key": key},
                            json=body)
    assert r1.status_code == 201

    # Force expiry. In tests, you can either:
    # (a) configure a 1-second TTL for tests and sleep, or
    # (b) directly delete the storage key.
    await redis.delete(storage_key_for(key))

    r2 = await client.post("/v1/charges",
                            headers={"Idempotency-Key": key},
                            json=body)
    assert r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]      # new charge, new ID
    assert count_charges_in_db(customer="cus_4") == 2
```

This is correct behavior. The client should not be reusing keys across the TTL boundary; if they do, they get a new operation. Document the TTL clearly so clients know.

## Test 5 — Failure-then-retry

**What it checks:** a 5xx response is not cached, so retry actually retries.

```python
async def test_5xx_does_not_poison_the_key():
    key = str(uuid.uuid4())
    body = {"amount": 100, "currency": "USD", "customer": "cus_5"}

    # Force the first call to fail with a transient error
    with mock_downstream_failure():
        r1 = await client.post("/v1/charges",
                                headers={"Idempotency-Key": key},
                                json=body)
    assert r1.status_code == 503

    # Second call: downstream is healthy, retry should succeed (not replay the 503)
    r2 = await client.post("/v1/charges",
                            headers={"Idempotency-Key": key},
                            json=body)
    assert r2.status_code == 201
    assert count_charges_in_db(customer="cus_5") == 1
```

If this test fails (r2 returns 503), your idempotency layer is caching 5xx responses. Every transient downstream failure becomes a permanent failure for that key. This is one of the most common production bugs in homegrown idempotency layers.

## Bonus tests worth running

- **Cross-tenant isolation.** Two tenants using the same key should see independent operations. Verify by creating the same key for tenant A and tenant B and confirming both execute.
- **In-flight TTL safeguard.** Insert an `IN_FLIGHT` record manually and verify it expires within the configured short TTL (so a crashed server doesn't block a key forever).
- **Header allowlist.** Replayed responses should not replay `Set-Cookie` or `Authorization` headers. Verify by setting one in the original response and checking it's absent in the replay.

## CI / load-testing layer

The unit-style tests above catch correctness bugs. To catch capacity and storage bugs, run a periodic load test:

- 1000 RPS for 60s with 50% retry rate (each successful response is "retried" once with the same key).
- Assert: the count of business-side effects equals the count of unique keys, not the count of HTTP requests.
- Monitor: idempotency store size, p99 latency on the atomic-insert path.

This is the only way to catch issues like "the unique constraint is on the wrong column" or "Redis cluster is hashing keys to wrong slots under load".
