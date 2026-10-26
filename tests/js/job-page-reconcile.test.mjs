//
// tests/js/job-page-reconcile.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// The standalone job page reconciles with an HTTP snapshot whenever its stream
// opens. A snapshot requested before a live update must not be applied after it.
import assert from "node:assert/strict";
import test from "node:test";

const streams = [];

class FakeEventSource {
    static CLOSED = 2;

    constructor(url) {
        this.url = url;
        this.readyState = 1;
        streams.push(this);
    }

    close() {
        this.readyState = FakeEventSource.CLOSED;
    }
}

function fakeElement(dataset = {}) {
    const classes = new Set();
    return {
        dataset,
        textContent: "",
        className: "",
        style: {},
        classList: {
            toggle(name, force) {
                if (force) classes.add(name);
                else classes.delete(name);
            },
            add: (name) => classes.add(name),
            remove: (name) => classes.delete(name),
            contains: (name) => classes.has(name),
        },
        setAttribute() { },
    };
}

const elements = new Map();
for (const id of [
    "status", "message", "bpmValue", "bpmRow", "bpmConfidence", "downloadLink",
    "progressBar", "progressRow", "progressLabel", "etaText", "statusText",
]) {
    elements.set(id, fakeElement());
}
elements.set("jobStatusScript", fakeElement({ jobId: "job-9", initialStatus: "downloading" }));

globalThis.EventSource = FakeEventSource;
globalThis.document = {
    cookie: "",
    documentElement: { dataset: { lalalMaxDurationSeconds: "600", statusContract: "{\"downloadable\":[\"analysis\",\"analysis_done\",\"done\"],\"cancellable\":[\"downloading\",\"processing\",\"queued\",\"transcoding\"],\"retryable\":[\"cancelled\",\"error\"],\"terminal\":[\"analysis_done\",\"cancelled\",\"done\",\"error\"],\"progress\":[\"downloading\",\"transcoding\"],\"meta\":{\"analysis\":{\"label\":\"Analyzing\",\"color\":\"primary\"},\"analysis_done\":{\"label\":\"Done\",\"color\":\"success\"},\"cancelled\":{\"label\":\"Cancelled\",\"color\":\"secondary\"},\"done\":{\"label\":\"Done\",\"color\":\"success\"},\"downloading\":{\"label\":\"Downloading\",\"color\":\"primary\"},\"error\":{\"label\":\"Error\",\"color\":\"danger\"},\"processing\":{\"label\":\"Preparing\",\"color\":\"primary\"},\"queued\":{\"label\":\"Queued\",\"color\":\"primary\"},\"transcoding\":{\"label\":\"Transcoding\",\"color\":\"primary\"}}}" } },
    getElementById: (id) => elements.get(id) ?? null,
    querySelector: () => null,
};
globalThis.window = {
    addEventListener() { },
    location: { replace() { } },
};

const snapshots = [];
globalThis.fetch = (url, options) => new Promise((resolve) => {
    snapshots.push({ url: String(url), options, resolve });
});

await import("../../app/static/js/job.js");

const settle = () => new Promise((resolve) => setImmediate(resolve));
const statusText = () => elements.get("statusText").textContent;

function answer(request, payload) {
    request.resolve({ ok: true, json: () => Promise.resolve(payload) });
}

test("a snapshot with no live update in between is applied", async () => {
    const stream = streams.at(-1);
    stream.onopen();
    assert.equal(snapshots.length, 1);
    assert.equal(snapshots[0].options.cache, "no-store");

    answer(snapshots[0], { id: "job-9", status: "processing" });
    await settle();

    assert.equal(statusText(), "Preparing");
});

test("a snapshot requested before a live terminal update does not revert it", async () => {
    const stream = streams.at(-1);
    stream.onopen();
    assert.equal(snapshots.length, 2);

    stream.onmessage({ data: JSON.stringify({ id: "job-9", status: "done" }) });
    assert.equal(statusText(), "Done");

    answer(snapshots[1], { id: "job-9", status: "processing" });
    await settle();

    assert.equal(statusText(), "Done");
    assert.equal(stream.readyState, FakeEventSource.CLOSED);
});
