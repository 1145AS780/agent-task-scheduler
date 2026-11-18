const argv = process.argv.slice(2);

let action = null;
const args = [];
for (let i = 0; i < argv.length; i += 1) {
  if (argv[i] === "--action") {
    action = argv[i + 1] ?? null;
    i += 1;
  } else {
    args.push(argv[i]);
  }
}

const payload = {
  plugin: "node-hello",
  action,
  args,
  node: process.version,
  cwd: process.cwd(),
};

console.log(JSON.stringify(payload, null, 2));

if (action === "fail") {
  process.exitCode = 3;
}