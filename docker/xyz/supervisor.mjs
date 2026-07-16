import {createHash, randomUUID} from "node:crypto";
import {spawn} from "node:child_process";
import {mkdir, open, readFile, rename, unlink} from "node:fs/promises";
import net from "node:net";

const reloadDir = process.env.RELOAD_DIR || "/reload";
const workspacePath = process.env.WORKSPACE_FILE || "/app/xyz/mapp-settings/workspace.json";
const positiveInteger = (value, fallback) => {
  const parsed = Number.parseInt(value, 10);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
};
const xyzPort = positiveInteger(process.env.PORT, 3000);
const startupTimeout = positiveInteger(
  process.env.XYZ_STARTUP_TIMEOUT_MS,
  30_000,
);
const pollInterval = positiveInteger(process.env.XYZ_RELOAD_POLL_MS, 500);
let child;
let stopping = false;
let targetGeneration;
let restarting = false;
let polling = false;
let poller;

const read = async (name, fallback = "0") => {
  try { return (await readFile(`${reloadDir}/${name}`, "utf8")).trim(); }
  catch { return fallback; }
};
const write = async (name, value) => {
  const target = `${reloadDir}/${name}`;
  const temporary = `${target}.${process.pid}.${randomUUID()}.tmp`;
  try {
    const handle = await open(temporary, "wx", 0o600);
    try {
      await handle.writeFile(`${value}\n`, "utf8");
      await handle.sync();
    } finally {
      await handle.close();
    }
    await rename(temporary, target);
  } finally {
    await unlink(temporary).catch(() => {});
  }
};
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const fingerprint = async () => createHash("sha256")
  .update(await readFile(workspacePath))
  .digest("hex");
const childIsCurrent = instance => (
  child === instance
  && instance.exitCode === null
  && !stopping
);
const xyzReady = () => new Promise(resolve => {
  const socket = net.connect(xyzPort, "127.0.0.1");
  let settled = false;
  const finish = ready => {
    if (settled) return;
    settled = true;
    socket.destroy();
    resolve(ready);
  };
  socket.setTimeout(500, () => finish(false));
  socket.once("connect", () => finish(true));
  socket.once("error", () => finish(false));
});

async function terminate(instance, signal = "SIGTERM") {
  if (!instance || instance.exitCode !== null) return;
  instance.kill(signal);
  await new Promise(resolve => {
    let force;
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(force);
      resolve();
    };
    instance.once("exit", finish);
    force = setTimeout(() => {
      if (instance.exitCode === null) {
        instance.kill("SIGKILL");
      } else {
        finish();
      }
    }, 10_000);
    if (instance.exitCode !== null) finish();
  });
}

async function start(generation) {
  targetGeneration = generation;
  await write("healthy", "false");
  if (stopping) return;
  const instance = spawn("node", ["express.js"], {
    stdio: "inherit",
    env: process.env,
  });
  child = instance;
  await write("started-at", new Date().toISOString());
  instance.once("exit", async () => {
    if (child !== instance) return;
    child = undefined;
    targetGeneration = undefined;
    await write("healthy", "false").catch(() => {});
  });

  const deadline = Date.now() + startupTimeout;
  while (childIsCurrent(instance)) {
    if (await xyzReady()) {
      if (!childIsCurrent(instance)) return;
      await write("workspace-fingerprint", await fingerprint());
      if (!childIsCurrent(instance)) return;
      await write("applied", generation);
      if (!childIsCurrent(instance)) return;
      await write("healthy", "true");
      return;
    }
    if (Date.now() >= deadline) {
      await terminate(instance);
      return;
    }
    await delay(250);
  }
}

async function restart(generation) {
  if (restarting || stopping) return;
  restarting = true;
  try {
    const previous = child;
    await terminate(previous);
    if (!stopping) await start(generation);
  } finally {
    restarting = false;
  }
}

async function reconcile() {
  if (polling || stopping) return;
  polling = true;
  try {
    const requested = await read("requested");
    if (requested !== targetGeneration) await restart(requested);
  } finally {
    polling = false;
  }
}

async function stop(signal) {
  if (stopping) return;
  stopping = true;
  if (poller) clearInterval(poller);
  await terminate(child, signal);
  process.exit(0);
}

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => {
    void stop(signal);
  });
}

await mkdir(reloadDir, {recursive: true});
const initial = await read("requested");
poller = setInterval(() => {
  void reconcile().catch(async error => {
    await write("healthy", "false").catch(() => {});
    console.error(`XYZ supervisor reconciliation failed: ${error.name}`);
  });
}, pollInterval);
await start(initial);
