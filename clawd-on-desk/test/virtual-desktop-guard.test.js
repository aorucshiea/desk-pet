"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert");
const { EventEmitter } = require("node:events");
const { PassThrough } = require("node:stream");

const {
  createVirtualDesktopGuard,
  parseDesktopLine,
} = require("../src/virtual-desktop-guard");

class FakeChild extends EventEmitter {
  constructor() {
    super();
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    this.killed = false;
  }
  kill() {
    this.killed = true;
  }
}

describe("virtual desktop guard", () => {
  it("parses watcher index lines", () => {
    assert.equal(parseDesktopLine("INDEX=2"), 2);
    assert.equal(parseDesktopLine(" INDEX=0 \r"), 0);
    assert.equal(parseDesktopLine("noise"), null);
    assert.equal(parseDesktopLine("INDEX=x"), null);
  });

  it("forwards desktop changes from the watcher", () => {
    const child = new FakeChild();
    const seen = [];
    const guard = createVirtualDesktopGuard({
      platform: "win32",
      spawnImpl: () => child,
      onDesktopChange: (index) => seen.push(index),
    });
    assert.equal(guard.start(), true);
    child.stdout.write("INDEX=1\nnoise\nINDEX=2\n");
    assert.deepEqual(seen, [1, 2]);
    guard.stop();
    assert.equal(child.killed, true);
  });

  it("does nothing outside Windows", () => {
    const guard = createVirtualDesktopGuard({ platform: "darwin" });
    assert.equal(guard.start(), false);
  });
});
