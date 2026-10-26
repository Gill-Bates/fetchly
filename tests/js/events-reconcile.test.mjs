//
// tests/js/events-reconcile.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// events.js keeps the job store in sync with two sources: the SSE stream and
// HTTP snapshots. A snapshot must never overwrite a newer live update, a hidden
// tab must re-sync when it returns, and one frame's updates must merge cleanly.
import assert from "node:assert/strict";
import test from "node:test";

const documentHandlers = new Map();
const windowHandlers = new Map();
const streams = [];
const frames = [];
const fetchCalls = [];
let fetchHandler = null;

class FakeEventSource {
    static CLOSED = 2;

    constructor(url) {
        this.url = url;
        this.readyState = 1;
        this.onopen = null;
        this.onmessage = null;
        this.onerror = null;
        streams.push(this);
    }

    close() {
        this.readyState = FakeEventSource.CLOSED;
    }
}

globalThis.EventSource = FakeEventSource;
// No list surfaces exist here; jobs.js only needs the classes for instanceof checks.
globalThis.Element = class Element { };
globalThis.HTMLElement = class HTMLElement extends globalThis.Element { };
globalThis.HTMLTableRowElement = class HTMLTableRowElement extends globalThis.HTMLElement { };
globalThis.document = {
    cookie: "",
    readyState: "loading",
    visibilityState: "visible",
    documentElement: { dataset: { lalalMaxDurationSeconds: "600", statusContract: "{\"downloadable\":[\"analysis\",\"analysis_done\",\"done\"],\"cancellable\":[\"downloading\",\"processing\",\"queued\",\"transcoding\"],\"retryable\":[\"cancelled\",\"error\"],\"terminal\":[\"analysis_done\",\"cancelled\",\"done\",\"error\"],\"progress\":[\"downloading\",\"transcoding\"],\"meta\":{\"analysis\":{\"label\":\"Analyzing\",\"color\":\"primary\"},\"analysis_done\":{\"label\":\"Done\",\"color\":\"success\"},\"cancelled\":{\"label\":\"Cancelled\",\"color\":\"secondary\"},\"done\":{\"label\":\"Done\",\"color\":\"success\"},\"downloading\":{\"label\":\"Downloading\",\"color\":\"primary\"},\"error\":{\"label\":\"Error\",\"color\":\"danger\"},\"processing\":{\"label\":\"Preparing\",\"color\":\"primary\"},\"queued\":{\"label\":\"Queued\",\"color\":\"primary\"},\"transcoding\":{\"label\":\"Transcoding\",\"color\":\"primary\"}}}" } },
    addEventListener(type, handler) {
        documentHandlers.set(type, handler);
    },
    dispatchEvent() { },
    querySelector() {
        return null;
    },
    getElementById() {
        return null;
    },
};
globalThis.window = {
    matchMedia: () => ({ matches: false, addEventListener() { } }),
    addEventListener(type, handler) {
        windowHandlers.set(type, handler);
    },
    removeEventListener() { },
    dispatchEvent() { },
    requestAnimationFrame(callback) {
        frames.push(callback);
        return frames.length;
    },
    cancelAnimationFrame() {
        frames.length = 0;
    },
    setTimeout: (...args) => setTimeout(...args),
    location: { replace() { } },
};
globalThis.fetch = (url, options) => {
    fetchCalls.push({ url: String(url), options });
    return fetchHandler(String(url));
};

const { connectEventStream } = await import("../../app/static/js/events.js");
const { getJobById, upsertJobSnapshot } = await import("../../app/static/js/jobs.js");

const respond = (body) => Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
const settle = () => new Promise((resolve) => setImmediate(resolve));

function flushFrames() {
    const pending = frames.splice(0);
    pending.forEach((callback) => callback());
}

function send(stream, payload) {
    stream.onmessage({ data: JSON.stringify(payload) });
    flushFrames();
}

function setVisibility(state) {
    document.visibilityState = state;
    documentHandlers.get("visibilitychange")();
}

/** Hide and show the tab, then open the replacement stream. */
function hideAndReturn() {
    setVisibility("hidden");
    setVisibility("visible");
    const stream = streams.at(-1);
    stream.onopen();
    return stream;
}

const jobsRequests = () => fetchCalls.filter((call) => call.url.startsWith("/api/jobs?"));

test("returning to a hidden tab reconciles exactly once on the next open", async () => {
    fetchHandler = () => respond([{ id: "vis-1", status: "done" }]);
    upsertJobSnapshot({ id: "vis-1", status: "downloading", progress: 40 });

    const first = connectEventStream();
    first.onopen();
    await settle();
    assert.equal(jobsRequests().length, 0, "first open needs no snapshot");

    setVisibility("hidden");
    assert.equal(first.readyState, FakeEventSource.CLOSED);
    setVisibility("visible");
    streams.at(-1).onopen();
    await settle();

    assert.equal(jobsRequests().length, 1);
    assert.equal(getJobById("vis-1").status, "done");
    assert.equal(jobsRequests()[0].options.cache, "no-store");

    streams.at(-1).onopen();
    await settle();
    assert.equal(jobsRequests().length, 1, "a plain later open does not reconcile again");
});

test("a snapshot that arrives after a newer live update does not overwrite it", async () => {
    upsertJobSnapshot({ id: "race-1", status: "downloading", progress: 50 });
    const before = jobsRequests().length;

    let release;
    let calls = 0;
    fetchHandler = () => {
        calls += 1;
        if (calls === 1) {
            return new Promise((resolve) => {
                release = () => resolve({
                    ok: true,
                    json: () => Promise.resolve([{ id: "race-1", status: "processing" }]),
                });
            });
        }
        return respond([{ id: "race-1", status: "done" }]);
    };

    const stream = hideAndReturn();
    await settle();
    send(stream, { id: "race-1", status: "done", seq: 5 });
    assert.equal(getJobById("race-1").status, "done");

    release();
    await settle();
    await settle();

    assert.equal(getJobById("race-1").status, "done");
    assert.equal(jobsRequests().length - before, 2, "skipped entry triggers one re-run");
});

test("reconcile retries are bounded when a job keeps changing", async () => {
    upsertJobSnapshot({ id: "chatty-1", status: "downloading", progress: 1 });
    const before = jobsRequests().length;
    let seq = 100;

    fetchHandler = () => {
        seq += 1;
        send(streams.at(-1), { id: "chatty-1", status: "downloading", progress: seq - 100, seq });
        return respond([{ id: "chatty-1", status: "downloading", progress: 0 }]);
    };

    hideAndReturn();
    await settle();
    await settle();

    assert.equal(jobsRequests().length - before, 3);
    assert.notEqual(getJobById("chatty-1").progress, 0);
});

test("a status without progress drops the progress of an update coalesced in the same frame", () => {
    upsertJobSnapshot({ id: "merge-1", status: "queued" });
    const stream = streams.at(-1);

    stream.onmessage({ data: JSON.stringify({ id: "merge-1", status: "downloading", progress: 70 }) });
    stream.onmessage({ data: JSON.stringify({ id: "merge-1", status: "processing" }) });
    flushFrames();

    const job = getJobById("merge-1");
    assert.equal(job.status, "processing");
    assert.equal(job.progress ?? null, null);
});

test("a progress-bearing status keeps the coalesced progress", () => {
    upsertJobSnapshot({ id: "merge-2", status: "queued" });
    const stream = streams.at(-1);

    stream.onmessage({ data: JSON.stringify({ id: "merge-2", status: "downloading", progress: 70 }) });
    stream.onmessage({ data: JSON.stringify({ id: "merge-2", message: "still going" }) });
    flushFrames();

    assert.equal(getJobById("merge-2").progress, 70);
});
