// 以 Node 內建測試執行：node --test tests/web
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { MonitorConnection } = require(path.join(__dirname, "../../app/web/app.js"));

class FakeWebSocket {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    FakeWebSocket.instances.push(this);
  }
  send(data) { this.sent.push(data); }
}

function setup() {
  FakeWebSocket.instances = [];
  const timers = [];
  const statuses = [];
  const messages = [];
  const conn = new MonitorConnection({
    url: "ws://127.0.0.1:8000/ws/monitor",
    WebSocketImpl: FakeWebSocket,
    setTimeoutImpl: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeoutImpl: () => {},
    onStatus: (s, d) => statuses.push([s, d]),
    onMessage: (m) => messages.push(m),
  });
  return { conn, timers, statuses, messages };
}

test("斷線後依退避時間自動重連，連上後重設退避", () => {
  const { conn, timers, statuses } = setup();
  conn.connect();
  assert.equal(FakeWebSocket.instances.length, 1);
  FakeWebSocket.instances[0].onclose();
  assert.deepEqual(statuses.at(-1), ["closed", 1000]);
  timers.shift().fn();
  assert.equal(FakeWebSocket.instances.length, 2);
  assert.deepEqual(statuses.at(-1), ["reconnecting", null]);
  FakeWebSocket.instances[1].onclose();
  assert.equal(timers[0].ms, 2000);
  timers.shift().fn();
  FakeWebSocket.instances[2].onopen();
  assert.deepEqual(statuses.at(-1), ["open", null]);
  FakeWebSocket.instances[2].onclose();
  assert.equal(timers[0].ms, 1000);
});

test("退避時間上限 15 秒", () => {
  assert.equal(MonitorConnection.backoff(1), 1000);
  assert.equal(MonitorConnection.backoff(4), 8000);
  assert.equal(MonitorConnection.backoff(20), 15000);
});

test("轉交快照與錯誤訊息，忽略無效 JSON", () => {
  const { conn, messages } = setup();
  conn.connect();
  const ws = FakeWebSocket.instances[0];
  ws.onmessage({ data: '{"type":"error","message":"db down"}' });
  ws.onmessage({ data: "not json" });
  assert.deepEqual(messages, [{ type: "error", message: "db down" }]);
});

test("分頁在背景時暫停：關閉連線且不重連，回到前景立即重連", () => {
  const { conn, timers, statuses } = setup();
  conn.connect();
  const ws = FakeWebSocket.instances[0];
  let closed = 0;
  ws.close = () => { closed += 1; };
  ws.onopen();
  conn.pause();
  assert.equal(closed, 1);
  assert.deepEqual(statuses.at(-1), ["paused", null]);
  ws.onclose(); // 關閉事件不應排程重連
  assert.equal(timers.length, 0);
  conn.pause(); // 重複暫停沒有作用
  assert.equal(closed, 1);
  conn.resume();
  assert.equal(FakeWebSocket.instances.length, 2);
  assert.deepEqual(statuses.at(-1), ["connecting", null]);
  conn.resume(); // 沒有暫停時不會多開連線
  assert.equal(FakeWebSocket.instances.length, 2);
});
