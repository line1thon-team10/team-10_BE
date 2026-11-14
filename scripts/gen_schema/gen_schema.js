// schema/erd.dbml(효원님 ERD) → app/db/schema.py (SQLAlchemy Core 테이블 정의)
// ERD가 바뀌면 다시 실행해요:  cd scripts/gen_schema && npm install && node gen_schema.js
const fs = require('fs');
const path = require('path');
const { Parser } = require('@dbml/core');

const root = path.resolve(__dirname, '..', '..');
const src = fs.readFileSync(path.join(root, 'schema', 'erd.dbml'), 'utf8');
const schema = new Parser().parse(src, 'dbmlv2').schemas[0];
const storageConstraints = { checks: [], foreignKeys: {}, serverDefaults: {} };
const checkNames = new Set();
for (const filename of ['quiz-storage-constraints.json', 'proposal-storage-constraints.json']) {
  const configPath = path.join(root, 'schema', filename);
  if (!fs.existsSync(configPath)) continue;
  const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  for (const check of config.checks) {
    const key = `${check.table}.${check.name}`;
    if (checkNames.has(key)) throw new Error(`Duplicate CHECK: ${key}`);
    checkNames.add(key);
    storageConstraints.checks.push(check);
  }
  for (const [field, options] of Object.entries(config.foreignKeys)) {
    if (Object.hasOwn(storageConstraints.foreignKeys, field)) throw new Error(`Duplicate foreign key: ${field}`);
    storageConstraints.foreignKeys[field] = options;
  }
  for (const [field, value] of Object.entries(config.serverDefaults || {})) {
    if (Object.hasOwn(storageConstraints.serverDefaults, field)) throw new Error(`Duplicate server default: ${field}`);
    if (typeof value !== 'string' || value.includes('\u0000')) throw new Error(`Unsupported server default: ${field}`);
    storageConstraints.serverDefaults[field] = value;
  }
}

const TYPE = {
  uuid: 'UUID',
  varchar: 'String',
  text: 'Text',
  int: 'Integer',
  decimal: 'Numeric(asdecimal=False)',
  boolean: 'Boolean',
  date: 'Date',
  timestamptz: 'DateTime(timezone=True)',
  jsonb: 'JSONB',
};

// 외래키: "테이블.컬럼" → "대상테이블.대상컬럼"
const fks = {};
for (const ref of schema.refs) {
  const many = ref.endpoints.find((e) => e.relation === '*') || ref.endpoints[0];
  const one = ref.endpoints.find((e) => e !== many);
  many.fieldNames.forEach((f, i) => {
    fks[`${many.tableName}.${f}`] = `${one.tableName}.${one.fieldNames[i]}`;
  });
}

// 순환 참조(예: plan_change_requests ↔ plan_proposals)는 PostgreSQL에서 ALTER로 나중에 추가
const edges = {};
for (const [from, to] of Object.entries(fks)) {
  const a = from.split('.')[0];
  const b = to.split('.')[0];
  if (a !== b) (edges[a] ||= []).push({ b, key: from });
}
const useAlter = new Set();
const state = {};
function dfs(n) {
  state[n] = 1;
  for (const { b, key } of edges[n] || []) {
    if (state[b] === 1) useAlter.add(key);
    else if (!state[b]) dfs(b);
  }
  state[n] = 2;
}
schema.tables.forEach((t) => !state[t.name] && dfs(t.name));

const py = (v) => JSON.stringify(v);
function defaultOf(f) {
  const d = f.dbdefault;
  if (!d) return null;
  if (d.type === 'boolean') return d.value === 'true' || d.value === true ? 'True' : 'False';
  if (d.type === 'number') return String(d.value);
  return py(d.value);
}

const out = [];
out.push('# 자동 생성 파일 — 직접 고치지 말고 schema/erd.dbml을 고친 뒤 scripts/gen_schema/gen_schema.js를 다시 실행하세요.');
out.push('# 원본: 효원님 ERD (OpenAPI 계약 0.7.0). 집중 부분 유일 인덱스는 schema/focus-indexes.json에서 생성.');
out.push(`from sqlalchemy import (Boolean, ${storageConstraints.checks.length ? 'CheckConstraint, ' : ''}Column, Date, DateTime, ForeignKey, Index, Integer, JSON, MetaData,`);
out.push('                        Numeric, PrimaryKeyConstraint, String, Table, Text, Uuid, text)');
out.push('from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB');
out.push('');
out.push('metadata = MetaData()');
out.push("UUID = Uuid(as_uuid=False)  # 파이썬에서는 문자열로 다뤄요");
out.push("JSONB = JSON().with_variant(PG_JSONB(), 'postgresql')");
out.push('');

for (const t of schema.tables) {
  const compositePk = t.indexes.find((i) => i.pk);
  out.push(`${t.name} = Table(`);
  out.push(`    ${py(t.name)}, metadata,`);
  for (const f of t.fields) {
    const args = [py(f.name), TYPE[f.type.type_name] || 'String'];
    const fk = fks[`${t.name}.${f.name}`];
    if (fk) {
      const alter = useAlter.has(`${t.name}.${f.name}`);
      const ondelete = storageConstraints.foreignKeys[`${t.name}.${f.name}`]?.ondelete;
      if (ondelete && ondelete !== 'CASCADE') throw new Error(`Unsupported ondelete: ${ondelete}`);
      args.push(`ForeignKey(${py(fk)}${alter ? `, use_alter=True, name=${py(`fk_${t.name}_${f.name}`)}` : ''}${ondelete ? `, ondelete=${py(ondelete)}` : ''})`);
    }
    if (f.pk) args.push('primary_key=True');
    args.push(`nullable=${f.pk || f.not_null ? 'False' : 'True'}`);
    if (f.unique && !f.pk) args.push('unique=True');
    const d = defaultOf(f);
    if (d !== null) args.push(`default=${d}`);
    if (Object.hasOwn(storageConstraints.serverDefaults, `${t.name}.${f.name}`)) {
      // SQLAlchemy quotes this literal string; this is not a raw SQL expression.
      args.push(`server_default=${py(storageConstraints.serverDefaults[`${t.name}.${f.name}`])}`);
    }
    out.push(`    Column(${args.join(', ')}),`);
  }
  if (compositePk) {
    out.push(`    PrimaryKeyConstraint(${compositePk.columns.map((c) => py(c.value)).join(', ')}),`);
  }
  for (const check of storageConstraints.checks.filter(check => check.table === t.name)) {
    out.push(`    CheckConstraint(${py(check.sql)}, name=${py(check.name)}),`);
  }
  t.indexes
    .filter((i) => !i.pk)
    .forEach((i, n) => {
      const cols = i.columns.map((c) => py(c.value)).join(', ');
      out.push(`    Index(${py(`ix_${t.name}_${n + 1}`)}, ${cols}${i.unique ? ', unique=True' : ''}),`);
    });
  out.push(')');
  out.push('');
}

for (const check of storageConstraints.checks) {
  if (!schema.tables.some(table => table.name === check.table)) throw new Error(`Unknown CHECK table: ${check.table}`);
}
for (const field of Object.keys(storageConstraints.foreignKeys)) {
  if (!fks[field]) throw new Error(`Unknown foreign key: ${field}`);
}
for (const field of Object.keys(storageConstraints.serverDefaults)) {
  if (!schema.tables.some(table => table.fields.some(column => `${table.name}.${column.name}` === field))) {
    throw new Error(`Unknown server default column: ${field}`);
  }
}

// DBML에서 표현할 수 없는 집중 부분 인덱스도 재생성 시 보존한다.
const focusIndexes = JSON.parse(fs.readFileSync(path.join(root, 'schema', 'focus-indexes.json'), 'utf8'));
for (const index of focusIndexes) {
  const columns = index.columns.map(c => `${index.table}.c.${c}`).join(', ');
  out.push(`Index(${py(index.name)}, ${columns}, unique=True,`);
  out.push(`      postgresql_where=text(${py(index.where)}), sqlite_where=text(${py(index.where)}))`);
}
out.push('');
fs.writeFileSync(path.join(root, 'app', 'db', 'schema.py'), out.join('\n'));
console.log(`tables: ${schema.tables.length}, fks: ${Object.keys(fks).length}, use_alter: ${[...useAlter].join(', ') || '-'}`);
