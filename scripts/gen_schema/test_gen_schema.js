const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'quiz-schema-generator-'));
try {
  for (const directory of ['schema', 'app/db', 'scripts/gen_schema']) {
    fs.mkdirSync(path.join(fixture, directory), { recursive: true });
  }
  const generator = path.join(fixture, 'scripts/gen_schema/gen_schema.js');
  fs.copyFileSync(path.join(__dirname, 'gen_schema.js'), generator);
  fs.writeFileSync(path.join(fixture, 'schema/erd.dbml'), `
Table parent {
  id uuid [pk]
}
Table child {
  id uuid [pk]
  parent_id uuid [not null, ref: > parent.id]
  revision int [not null]
}
`);
  fs.writeFileSync(path.join(fixture, 'schema/focus-indexes.json'), '[]');
  const configPath = path.join(fixture, 'schema/quiz-storage-constraints.json');
  const config = { checks: [{ table: 'child', name: 'ck_child_revision', sql: 'revision > 0' }],
    foreignKeys: { 'child.parent_id': { ondelete: 'CASCADE' } } };
  const dependencyRoot = path.resolve(path.dirname(require.resolve('@dbml/core/package.json')), '../..');
  const generate = () => spawnSync(process.execPath, [generator], {
    encoding: 'utf8', env: { ...process.env, NODE_PATH: dependencyRoot },
  });
  fs.writeFileSync(configPath, JSON.stringify(config));
  let result = generate();
  assert.equal(result.status, 0, result.stderr);
  let output = fs.readFileSync(path.join(fixture, 'app/db/schema.py'), 'utf8');
  assert.match(output, /ForeignKey\("parent.id", ondelete="CASCADE"\)/);
  assert.match(output, /CheckConstraint\("revision > 0", name="ck_child_revision"\)/);
  const proposalConfigPath = path.join(fixture, 'schema/proposal-storage-constraints.json');
  fs.writeFileSync(proposalConfigPath, JSON.stringify({
    checks: [{ table: 'child', name: 'ck_child_revision_limit', sql: 'revision < 10' }], foreignKeys: {},
  }));
  result = generate();
  assert.equal(result.status, 0, result.stderr);
  output = fs.readFileSync(path.join(fixture, 'app/db/schema.py'), 'utf8');
  assert.match(output, /CheckConstraint\("revision > 0", name="ck_child_revision"\)/);
  assert.match(output, /CheckConstraint\("revision < 10", name="ck_child_revision_limit"\)/);
  assert.match(output, /ForeignKey\("parent.id", ondelete="CASCADE"\)/);
  for (const duplicate of [config, { checks: [], foreignKeys: config.foreignKeys }]) {
    fs.writeFileSync(proposalConfigPath, JSON.stringify(duplicate));
    assert.notEqual(generate().status, 0);
  }
  fs.unlinkSync(proposalConfigPath);
  fs.unlinkSync(configPath);
  result = generate();
  assert.equal(result.status, 0, result.stderr);
  output = fs.readFileSync(path.join(fixture, 'app/db/schema.py'), 'utf8');
  assert.doesNotMatch(output, /ondelete|CheckConstraint/);
  for (const invalid of [
    { ...config, checks: [{ table: 'missing', name: 'bad', sql: '1=1' }] },
    { ...config, foreignKeys: { 'child.missing': { ondelete: 'CASCADE' } } },
    { ...config, foreignKeys: { 'child.parent_id': { ondelete: 'SET NULL' } } },
  ]) {
    fs.writeFileSync(configPath, JSON.stringify(invalid));
    assert.notEqual(generate().status, 0);
  }
  console.log('schema generator: merged CHECK/CASCADE, backward compatibility and invalid configuration passed');
} finally {
  fs.rmSync(fixture, { recursive: true, force: true });
}
