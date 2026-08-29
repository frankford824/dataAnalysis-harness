use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{BufReader, Read, Write};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::Command as ProcessCommand;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, anyhow, bail};
use arrow_array::{ArrayRef, RecordBatch, StringArray, UInt64Array};
use arrow_schema::{DataType, Field as ArrowField, Schema as ArrowSchema};
use axum::extract::{Query, State};
use axum::routing::get;
use axum::{Json, Router};
use calamine::{Data, DataRef, Reader, Xlsx, open_workbook, open_workbook_auto};
use chrono::Utc;
use clap::{Parser, Subcommand};
use encoding_rs::GBK;
use parquet::arrow::ArrowWriter;
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::properties::WriterProperties;
use rusqlite::{Connection, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tantivy::collector::TopDocs;
use tantivy::query::{BooleanQuery, Occur, Query as TantivyQuery, QueryParser, TermQuery};
use tantivy::schema::{
    FAST, INDEXED, IndexRecordOption, STORED, STRING, Schema, TantivyDocument, TextFieldIndexing,
    TextOptions, Value as TantivyValue,
};
use tantivy::tokenizer::NgramTokenizer;
use tantivy::{Index, IndexReader, IndexWriter, Order, ReloadPolicy, Term, doc};
use tokio::time;
use walkdir::WalkDir;

const CATALOG_SCHEMA: &str = r#"
pragma journal_mode=wal;
pragma synchronous=full;
create table if not exists file_catalog (
  path text primary key,
  sha256 text not null,
  size integer not null,
  mtime_ns integer not null,
  platform text not null default '',
  store_id text not null default '',
  source text not null default '',
  authority text not null default 'calculation',
  state text not null,
  rows integer not null default 0,
  sheets integer not null default 0,
  parquet_path text not null default '',
  error text not null default '',
  indexed_at text not null default '',
  last_seen_generation integer not null default 0,
  last_changed integer not null default 0,
  missing_scans integer not null default 0,
  missing_since integer
);
create index if not exists file_catalog_sha on file_catalog(sha256);
create index if not exists file_catalog_state on file_catalog(state);
create table if not exists scan_meta (
  id integer primary key check(id=1),
  generation integer not null default 0,
  last_started text not null default '',
  last_completed text not null default '',
  root_reachable integer not null default 0,
  last_error text not null default ''
);
insert or ignore into scan_meta(id) values(1);
create table if not exists jobs (
  id integer primary key autoincrement,
  kind text not null,
  path text not null,
  state text not null,
  created_at text not null,
  started_at text not null default '',
  finished_at text not null default '',
  error text not null default ''
);
create table if not exists network_sample (
  at integer primary key,
  mbps integer not null
);
create table if not exists hot_cache (
  sha256 text primary key,
  path text not null,
  size integer not null,
  last_access integer not null
);
"#;

const INDEX_DIR: &str = "tantivy-v2";
const HOT_LIMIT_BYTES: u64 = 32 * 1024 * 1024 * 1024;
const HOT_MAX_AGE_SECONDS: i64 = 30 * 86_400;

#[derive(Parser)]
#[command(
    name = "ledger-indexer",
    version,
    about = "NAS spreadsheet indexer for Ledger"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Init {
        #[arg(long)]
        data: PathBuf,
    },
    IndexFile {
        #[arg(long)]
        data: PathBuf,
        #[arg(long)]
        file: PathBuf,
        #[arg(long, default_value = "")]
        platform: String,
        #[arg(long, default_value = "")]
        store: String,
        #[arg(long, default_value = "")]
        source: String,
        #[arg(long, default_value = "calculation")]
        authority: String,
    },
    ScanOnce {
        #[arg(long)]
        root: PathBuf,
        #[arg(long)]
        data: PathBuf,
    },
    Search {
        #[arg(long)]
        data: PathBuf,
        #[arg(long)]
        query: String,
        #[arg(long, default_value_t = 50)]
        limit: usize,
    },
    Serve {
        #[arg(long)]
        root: PathBuf,
        #[arg(long)]
        data: PathBuf,
        #[arg(long, default_value = "127.0.0.1:8765")]
        bind: SocketAddr,
    },
}

#[derive(Clone)]
struct AppState {
    root: PathBuf,
    data: PathBuf,
    search: Arc<SearchRuntime>,
}

#[derive(Debug, Clone, Serialize)]
struct RowDoc {
    file_sha: String,
    path: String,
    sheet: String,
    row_no: u64,
    cells_json: String,
    all_text: String,
}

#[derive(Debug, Clone, Serialize)]
struct IndexStats {
    sha256: String,
    bytes: u64,
    rows: u64,
    sheets: u64,
    elapsed_ms: u128,
    parquet_path: String,
}

#[derive(Debug, Deserialize)]
struct SearchArgs {
    q: String,
    limit: Option<usize>,
    store_id: Option<String>,
    platform: Option<String>,
    source: Option<String>,
}

#[derive(Debug, Deserialize)]
struct PreviewArgs {
    sha: String,
    sheet: Option<String>,
    offset: Option<usize>,
    limit: Option<usize>,
}

#[derive(Debug, Serialize)]
struct SearchHit {
    score: f32,
    file_sha: String,
    path: String,
    sheet: String,
    row_no: u64,
    platform: String,
    store_id: String,
    source: String,
    authority: String,
    matches: Vec<CellMatch>,
    snippet: String,
}

#[derive(Debug, Serialize)]
struct CellMatch {
    column_index: usize,
    value: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    match Cli::parse().command {
        Command::Init { data } => {
            init_data_dir(&data)?;
            println!("{}", json!({"ok": true, "data": data}));
        }
        Command::IndexFile {
            data,
            file,
            platform,
            store,
            source,
            authority,
        } => {
            println!(
                "{}",
                serde_json::to_string(&index_file(
                    &data, &file, &file, &platform, &store, &source, &authority, None,
                )?)?
            );
        }
        Command::ScanOnce { root, data } => println!("{}", scan_once(&root, &data)?),
        Command::Search { data, query, limit } => {
            println!(
                "{}",
                serde_json::to_string(&search_index(&data, &query, limit, None, None, None,)?)?
            );
        }
        Command::Serve { root, data, bind } => serve(root, data, bind).await?,
    }
    Ok(())
}

fn init_data_dir(data: &Path) -> Result<()> {
    for name in [INDEX_DIR, "parquet", "hot", "logs"] {
        fs::create_dir_all(data.join(name))?;
    }
    let conn = Connection::open(data.join("catalog.db"))?;
    conn.execute_batch(CATALOG_SCHEMA)?;
    // Forward-compatible for catalogs created by an earlier pre-release build.
    let _ = conn.execute(
        "alter table file_catalog add column last_changed integer not null default 0",
        [],
    );
    let index_path = data.join(INDEX_DIR);
    let fresh_index = !index_path.join("meta.json").exists();
    let _ = open_or_create_index(&index_path)?;
    if fresh_index {
        conn.execute(
            "update file_catalog set state='stabilizing',last_changed=0 where state='ready'",
            [],
        )?;
    }
    drop(conn);
    Ok(())
}

fn schema() -> Schema {
    let mut b = Schema::builder();
    b.add_text_field("uid", STRING | STORED);
    b.add_text_field("file_sha", STRING | STORED);
    b.add_text_field("path", STRING | STORED);
    b.add_text_field("sheet", STRING | STORED);
    b.add_u64_field("row_no", INDEXED | FAST | STORED);
    b.add_text_field("platform", STRING | STORED);
    b.add_text_field("store_id", STRING | STORED);
    b.add_text_field("source", STRING | STORED);
    b.add_text_field("authority", STRING | STORED);
    b.add_text_field("identifier", STRING);
    let indexing = TextFieldIndexing::default()
        .set_tokenizer("ngram2")
        .set_index_option(IndexRecordOption::WithFreqsAndPositions);
    // The searchable text already exists in the Parquet row and cells_json.  Keeping another
    // stored copy in Tantivy made the real 106 MB workbook index hundreds of MB larger.
    b.add_text_field(
        "all_text",
        TextOptions::default().set_indexing_options(indexing),
    );
    b.add_text_field("cells_json", STORED);
    b.build()
}

fn open_or_create_index(path: &Path) -> Result<Index> {
    fs::create_dir_all(path)?;
    let index = if path.join("meta.json").exists() {
        Index::open_in_dir(path)?
    } else {
        Index::create_in_dir(path, schema())?
    };
    // Overlapping bigrams also cover longer Chinese substrings while producing materially fewer
    // postings than a combined 2/3-gram index. Exact containment is checked after retrieval.
    index
        .tokenizers()
        .register("ngram2", NgramTokenizer::all_ngrams(2, 2)?);
    Ok(index)
}

fn hash_file(path: &Path) -> Result<String> {
    let mut f = BufReader::new(File::open(path)?);
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1024 * 1024];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(hex::encode(h.finalize()))
}

fn decode_csv(path: &Path) -> Result<String> {
    let bytes = fs::read(path)?;
    decode_csv_bytes(&bytes)
}

fn decode_csv_bytes(bytes: &[u8]) -> Result<String> {
    let without_bom = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF]).unwrap_or(&bytes);
    if let Ok(text) = std::str::from_utf8(without_bom) {
        return Ok(text.to_string());
    }
    let (decoded, _encoding, had_errors) = GBK.decode(&bytes);
    if had_errors {
        bail!("CSV is neither valid UTF-8 nor losslessly decodable as GB18030/GBK");
    }
    Ok(decoded.into_owned())
}

struct TemporaryArtifact {
    path: PathBuf,
    keep: bool,
}

impl Drop for TemporaryArtifact {
    fn drop(&mut self) {
        if !self.keep {
            let _ = fs::remove_file(&self.path);
        }
    }
}

fn modified_ns(metadata: &fs::Metadata) -> i64 {
    metadata
        .modified()
        .ok()
        .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
        .map(|value| value.as_nanos() as i64)
        .unwrap_or_default()
}

fn cache_source(
    data: &Path,
    source: &Path,
    expected_sha: Option<&str>,
) -> Result<(String, PathBuf)> {
    let conn = Connection::open(data.join("catalog.db"))?;
    if let Some(expected) = expected_sha.filter(|value| !value.is_empty()) {
        let cached = conn
            .query_row(
                "select path,size from hot_cache where sha256=?",
                [expected],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)),
            )
            .ok();
        if let Some((path, size)) = cached {
            let path = PathBuf::from(path);
            if path.is_file() && fs::metadata(&path)?.len() == size as u64 {
                conn.execute(
                    "update hot_cache set last_access=? where sha256=?",
                    params![unix_seconds(), expected],
                )?;
                return Ok((expected.to_string(), path));
            }
        }
    }

    let before = fs::metadata(source)?;
    let incoming = data.join("hot").join(format!(
        ".incoming-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos(),
    ));
    let mut temporary = TemporaryArtifact {
        path: incoming,
        keep: false,
    };
    let mut reader = BufReader::new(File::open(source)?);
    let mut writer = File::create(&temporary.path)?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let read = reader.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
        writer.write_all(&buffer[..read])?;
    }
    writer.sync_all()?;
    drop(writer);
    let sha = hex::encode(digest.finalize());
    if expected_sha.is_some_and(|expected| !expected.is_empty() && expected != sha) {
        bail!("source content no longer matches its catalog SHA");
    }
    let after = fs::metadata(source)?;
    if before.len() != after.len() || modified_ns(&before) != modified_ns(&after) {
        bail!("source changed while filling the hot cache");
    }
    let extension = source
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("bin")
        .to_ascii_lowercase();
    let target = data
        .join("hot")
        .join(&sha[..2])
        .join(format!("{sha}.{extension}"));
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)?;
    }
    if target.exists() {
        fs::remove_file(&temporary.path)?;
    } else {
        fs::rename(&temporary.path, &target)?;
    }
    temporary.keep = true;
    conn.execute(
        "insert into hot_cache(sha256,path,size,last_access) values(?,?,?,?) on conflict(sha256) do update set path=excluded.path,size=excluded.size,last_access=excluded.last_access",
        params![sha, target.to_string_lossy(), before.len() as i64, unix_seconds()],
    )?;
    Ok((sha, target))
}

fn prune_hot_cache(data: &Path) -> Result<()> {
    let root = fs::canonicalize(data.join("hot"))?;
    let conn = Connection::open(data.join("catalog.db"))?;
    let mut statement = conn
        .prepare("select sha256,path,size,last_access from hot_cache order by last_access asc")?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, i64>(3)?,
            ))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let mut total = rows.iter().map(|row| row.2.max(0) as u64).sum::<u64>();
    let cutoff = unix_seconds() - HOT_MAX_AGE_SECONDS;
    for (sha, path, size, last_access) in rows {
        if last_access >= cutoff && total <= HOT_LIMIT_BYTES {
            continue;
        }
        let candidate = PathBuf::from(path);
        let safe = candidate
            .parent()
            .and_then(|parent| fs::canonicalize(parent).ok())
            .is_some_and(|parent| parent.starts_with(&root));
        if safe {
            let _ = fs::remove_file(&candidate);
        }
        conn.execute("delete from hot_cache where sha256=?", [sha])?;
        total = total.saturating_sub(size.max(0) as u64);
    }
    Ok(())
}

fn arrow_schema() -> Arc<ArrowSchema> {
    Arc::new(ArrowSchema::new(vec![
        ArrowField::new("file_sha", DataType::Utf8, false),
        ArrowField::new("path", DataType::Utf8, false),
        ArrowField::new("sheet", DataType::Utf8, false),
        ArrowField::new("row_no", DataType::UInt64, false),
        ArrowField::new("cells_json", DataType::Utf8, false),
        ArrowField::new("all_text", DataType::Utf8, false),
    ]))
}

fn write_batch(writer: &mut ArrowWriter<File>, rows: &mut Vec<RowDoc>) -> Result<()> {
    if rows.is_empty() {
        return Ok(());
    }
    let arrays: Vec<ArrayRef> = vec![
        Arc::new(StringArray::from_iter_values(
            rows.iter().map(|r| r.file_sha.as_str()),
        )),
        Arc::new(StringArray::from_iter_values(
            rows.iter().map(|r| r.path.as_str()),
        )),
        Arc::new(StringArray::from_iter_values(
            rows.iter().map(|r| r.sheet.as_str()),
        )),
        Arc::new(UInt64Array::from_iter_values(rows.iter().map(|r| r.row_no))),
        Arc::new(StringArray::from_iter_values(
            rows.iter().map(|r| r.cells_json.as_str()),
        )),
        Arc::new(StringArray::from_iter_values(
            rows.iter().map(|r| r.all_text.as_str()),
        )),
    ];
    writer.write(&RecordBatch::try_new(arrow_schema(), arrays)?)?;
    rows.clear();
    Ok(())
}

fn data_ref_text(value: &DataRef<'_>) -> String {
    match value {
        DataRef::Empty => String::new(),
        DataRef::Int(v) => v.to_string(),
        DataRef::Float(v) if v.fract() == 0.0 => format!("{v:.0}"),
        DataRef::Float(v) => v.to_string(),
        DataRef::String(v) => v.clone(),
        DataRef::SharedString(v) => (*v).to_string(),
        DataRef::Bool(v) => v.to_string(),
        DataRef::DateTime(v) => v.to_string(),
        DataRef::DateTimeIso(v) | DataRef::DurationIso(v) => v.clone(),
        DataRef::Error(v) => format!("{v:?}"),
    }
}

fn data_text(value: &Data) -> String {
    match value {
        Data::Empty => String::new(),
        Data::Int(value) => value.to_string(),
        Data::Float(value) if value.fract() == 0.0 => format!("{value:.0}"),
        Data::Float(value) => value.to_string(),
        Data::String(value) => value.clone(),
        Data::Bool(value) => value.to_string(),
        Data::DateTime(value) => value.to_string(),
        Data::DateTimeIso(value) | Data::DurationIso(value) => value.clone(),
        Data::Error(value) => format!("{value:?}"),
    }
}

#[derive(Clone, Copy)]
struct IndexFields {
    uid: tantivy::schema::Field,
    file_sha: tantivy::schema::Field,
    path: tantivy::schema::Field,
    sheet: tantivy::schema::Field,
    row_no: tantivy::schema::Field,
    platform: tantivy::schema::Field,
    store_id: tantivy::schema::Field,
    source: tantivy::schema::Field,
    authority: tantivy::schema::Field,
    identifier: tantivy::schema::Field,
    all_text: tantivy::schema::Field,
    cells_json: tantivy::schema::Field,
}

struct SearchRuntime {
    index: Index,
    reader: IndexReader,
    fields: IndexFields,
}

fn open_search_runtime(data: &Path) -> Result<SearchRuntime> {
    let index = open_or_create_index(&data.join(INDEX_DIR))?;
    let fields = IndexFields::from_schema(&index.schema())?;
    let reader = index
        .reader_builder()
        .reload_policy(ReloadPolicy::OnCommitWithDelay)
        .try_into()?;
    reader.reload()?;
    Ok(SearchRuntime {
        index,
        reader,
        fields,
    })
}

impl IndexFields {
    fn from_schema(schema: &Schema) -> Result<Self> {
        let field = |name: &str| {
            schema
                .get_field(name)
                .map_err(|_| anyhow!("missing field {name}"))
        };
        Ok(Self {
            uid: field("uid")?,
            file_sha: field("file_sha")?,
            path: field("path")?,
            sheet: field("sheet")?,
            row_no: field("row_no")?,
            platform: field("platform")?,
            store_id: field("store_id")?,
            source: field("source")?,
            authority: field("authority")?,
            identifier: field("identifier")?,
            all_text: field("all_text")?,
            cells_json: field("cells_json")?,
        })
    }
}

fn is_identifier(value: &str) -> bool {
    let value = value.trim();
    (6..=128).contains(&value.len())
        && value.bytes().any(|byte| byte.is_ascii_digit())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn identifier_tokens(cells: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    for cell in cells {
        for token in cell.split(|ch: char| !(ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_')))
        {
            if is_identifier(token) {
                out.push(token.to_ascii_lowercase());
            }
        }
    }
    out.sort_unstable();
    out.dedup();
    out
}

#[allow(clippy::too_many_arguments)]
fn flush_row(
    sha: &str,
    path: &Path,
    sheet: &str,
    row_no: u64,
    cells: &mut Vec<String>,
    parquet_rows: &mut Vec<RowDoc>,
    index_writer: &mut IndexWriter,
    fields: &IndexFields,
    platform: &str,
    store: &str,
    source: &str,
    authority: &str,
) -> Result<()> {
    while cells.last().is_some_and(|v| v.is_empty()) {
        cells.pop();
    }
    if cells.is_empty() {
        return Ok(());
    }
    let all_text = cells
        .iter()
        .filter(|v| !v.is_empty())
        .cloned()
        .collect::<Vec<_>>()
        .join("\t");
    let cells_json = serde_json::to_string(cells)?;
    let path_text = path.to_string_lossy().to_string();
    let uid = format!("{sha}:{sheet}:{row_no}");
    let mut document = doc!(
        fields.uid => uid, fields.file_sha => sha.to_string(), fields.path => path_text.clone(),
        fields.sheet => sheet.to_string(), fields.row_no => row_no,
        fields.platform => platform.to_string(), fields.store_id => store.to_string(),
        fields.source => source.to_string(), fields.authority => authority.to_string(),
        fields.all_text => all_text.clone(), fields.cells_json => cells_json.clone(),
    );
    for identifier in identifier_tokens(cells) {
        document.add_text(fields.identifier, identifier);
    }
    index_writer.add_document(document)?;
    parquet_rows.push(RowDoc {
        file_sha: sha.to_string(),
        path: path_text,
        sheet: sheet.to_string(),
        row_no,
        cells_json,
        all_text,
    });
    cells.clear();
    Ok(())
}

fn index_file(
    data: &Path,
    path: &Path,
    content_path: &Path,
    platform: &str,
    store: &str,
    source: &str,
    authority: &str,
    known_sha: Option<String>,
) -> Result<IndexStats> {
    init_data_dir(data)?;
    let started = Instant::now();
    let metadata = fs::metadata(path).with_context(|| format!("stat {}", path.display()))?;
    let sha = known_sha.unwrap_or(hash_file(path)?);
    let parquet_path = data.join("parquet").join(format!("{sha}.parquet"));
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let mut temporary = TemporaryArtifact {
        path: data
            .join("parquet")
            .join(format!(".{sha}.{}.{}.part", std::process::id(), unique)),
        keep: false,
    };
    let props = WriterProperties::builder()
        .set_compression(Compression::ZSTD(ZstdLevel::default()))
        .build();
    let mut parquet =
        ArrowWriter::try_new(File::create(&temporary.path)?, arrow_schema(), Some(props))?;
    let index = open_or_create_index(&data.join(INDEX_DIR))?;
    let fields = IndexFields::from_schema(&index.schema())?;
    let mut index_writer = index.writer_with_num_threads(4, 256_000_000)?;
    index_writer.delete_term(Term::from_field_text(fields.file_sha, &sha));
    let mut buffered = Vec::with_capacity(4096);
    let mut row_count = 0u64;
    let mut sheet_count = 0u64;
    let extension = content_path
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    match extension.as_str() {
        "xlsx" | "xlsm" => {
            let mut workbook: Xlsx<_> = open_workbook(content_path)?;
            for sheet_name in workbook.sheet_names().to_vec() {
                sheet_count += 1;
                let mut reader = workbook.worksheet_cells_reader(&sheet_name)?;
                let mut current_row = None;
                let mut cells = Vec::<String>::new();
                while let Some(cell) = reader.next_cell_with_formula()? {
                    if current_row != Some(cell.pos.0) {
                        if let Some(row) = current_row {
                            flush_row(
                                &sha,
                                path,
                                &sheet_name,
                                row as u64 + 1,
                                &mut cells,
                                &mut buffered,
                                &mut index_writer,
                                &fields,
                                platform,
                                store,
                                source,
                                authority,
                            )?;
                            row_count += 1;
                            if buffered.len() >= 4096 {
                                write_batch(&mut parquet, &mut buffered)?;
                            }
                        }
                        current_row = Some(cell.pos.0);
                    }
                    let col = cell.pos.1 as usize;
                    if cells.len() <= col {
                        cells.resize(col + 1, String::new());
                    }
                    let mut text = data_ref_text(&cell.value);
                    if let Some(formula) = cell.formula {
                        if !formula.is_empty() {
                            text.push_str(" =");
                            text.push_str(&formula);
                        }
                    }
                    cells[col] = text;
                }
                if let Some(row) = current_row {
                    flush_row(
                        &sha,
                        path,
                        &sheet_name,
                        row as u64 + 1,
                        &mut cells,
                        &mut buffered,
                        &mut index_writer,
                        &fields,
                        platform,
                        store,
                        source,
                        authority,
                    )?;
                    row_count += 1;
                }
            }
        }
        "csv" => {
            sheet_count = 1;
            let decoded = decode_csv(content_path)?;
            let mut csv = csv::ReaderBuilder::new()
                .flexible(true)
                .from_reader(decoded.as_bytes());
            let mut cells = csv
                .headers()?
                .iter()
                .map(str::to_string)
                .collect::<Vec<_>>();
            flush_row(
                &sha,
                path,
                "CSV",
                1,
                &mut cells,
                &mut buffered,
                &mut index_writer,
                &fields,
                platform,
                store,
                source,
                authority,
            )?;
            row_count += 1;
            for (idx, record) in csv.records().enumerate() {
                let mut cells = record?.iter().map(str::to_string).collect::<Vec<_>>();
                flush_row(
                    &sha,
                    path,
                    "CSV",
                    idx as u64 + 2,
                    &mut cells,
                    &mut buffered,
                    &mut index_writer,
                    &fields,
                    platform,
                    store,
                    source,
                    authority,
                )?;
                row_count += 1;
                if buffered.len() >= 4096 {
                    write_batch(&mut parquet, &mut buffered)?;
                }
            }
        }
        "xls" | "xlsb" => {
            if metadata.len() > 512 * 1024 * 1024 {
                bail!("legacy workbook exceeds the 512 MiB buffered-parse safety limit");
            }
            let mut workbook = open_workbook_auto(content_path)?;
            for sheet_name in workbook.sheet_names().to_vec() {
                let range = workbook.worksheet_range(&sheet_name)?;
                let (height, width) = range.get_size();
                if height.saturating_mul(width) > 20_000_000 {
                    bail!("legacy sheet exceeds the 20 million cell safety limit: {sheet_name}");
                }
                sheet_count += 1;
                let start_row = range.start().map(|position| position.0 as u64).unwrap_or(0);
                for (index, row) in range.rows().enumerate() {
                    let mut cells = row.iter().map(data_text).collect::<Vec<_>>();
                    flush_row(
                        &sha,
                        path,
                        &sheet_name,
                        start_row + index as u64 + 1,
                        &mut cells,
                        &mut buffered,
                        &mut index_writer,
                        &fields,
                        platform,
                        store,
                        source,
                        authority,
                    )?;
                    row_count += 1;
                    if buffered.len() >= 4096 {
                        write_batch(&mut parquet, &mut buffered)?;
                    }
                }
            }
        }
        _ => bail!("unsupported extension: {extension}"),
    }
    write_batch(&mut parquet, &mut buffered)?;
    parquet.close()?;
    let final_metadata = fs::metadata(path)?;
    let final_sha = hash_file(path)?;
    if final_sha != sha
        || final_metadata.len() != metadata.len()
        || modified_ns(&final_metadata) != modified_ns(&metadata)
    {
        bail!("file changed while it was being parsed; retry after it is stable");
    }
    if parquet_path.exists() {
        fs::remove_file(&temporary.path)?;
    } else {
        fs::rename(&temporary.path, &parquet_path)?;
    }
    temporary.keep = true;
    index_writer.commit()?;
    let mtime_ns = modified_ns(&metadata);
    let conn = Connection::open(data.join("catalog.db"))?;
    conn.execute(
        "insert into file_catalog(path,sha256,size,mtime_ns,platform,store_id,source,authority,state,rows,sheets,parquet_path,indexed_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?)\
         on conflict(path) do update set sha256=excluded.sha256,size=excluded.size,mtime_ns=excluded.mtime_ns,platform=excluded.platform,store_id=excluded.store_id,source=excluded.source,authority=excluded.authority,state='ready',rows=excluded.rows,sheets=excluded.sheets,parquet_path=excluded.parquet_path,error='',indexed_at=excluded.indexed_at,missing_scans=0,missing_since=null",
        params![path.to_string_lossy(), sha, metadata.len() as i64, mtime_ns, platform,
            store, source, authority, "ready", row_count as i64, sheet_count as i64,
            parquet_path.to_string_lossy(), Utc::now().to_rfc3339()],
    )?;
    Ok(IndexStats {
        sha256: sha,
        bytes: metadata.len(),
        rows: row_count,
        sheets: sheet_count,
        elapsed_ms: started.elapsed().as_millis(),
        parquet_path: parquet_path.to_string_lossy().to_string(),
    })
}

fn supported(path: &Path) -> bool {
    matches!(
        path.extension()
            .and_then(|s| s.to_str())
            .map(str::to_ascii_lowercase)
            .as_deref(),
        Some("xlsx" | "xlsm" | "xls" | "xlsb" | "csv")
    ) && !path
        .file_name()
        .and_then(|s| s.to_str())
        .is_some_and(|n| n.starts_with("~$") || n.ends_with(".tmp") || n.ends_with(".part"))
}

fn infer_scope(root: &Path, path: &Path) -> (String, String, String, String) {
    let mut parts = path
        .strip_prefix(root)
        .unwrap_or(path)
        .iter()
        .map(|p| p.to_string_lossy().to_string())
        .collect::<Vec<_>>();
    if parts
        .first()
        .is_some_and(|p| p == "00_上传区" || p == "10_已接收")
    {
        parts.remove(0);
    }
    let authority = if parts.iter().any(|p| p.contains("手工")) {
        "search_only"
    } else {
        "calculation"
    };
    if parts.first().is_some_and(|p| p == "00_全公司共享") {
        return (
            "shared".into(),
            "__shared__".into(),
            parts.get(1).cloned().unwrap_or_default(),
            authority.into(),
        );
    }
    let platform = parts.first().cloned().unwrap_or_default();
    let store_folder = parts.get(1).cloned().unwrap_or_default();
    let store_id = store_folder
        .rsplit_once('[')
        .and_then(|(_, tail)| tail.strip_suffix(']'))
        .unwrap_or(&store_folder)
        .to_string();
    (
        platform,
        store_id,
        parts.get(2).cloned().unwrap_or_default(),
        authority.into(),
    )
}

fn scan_once(root: &Path, data: &Path) -> Result<Value> {
    init_data_dir(data)?;
    if !root.exists() {
        bail!("NAS root not reachable: {}", root.display());
    }
    if let Some(mbps) = link_speed_mbps() {
        let conn = Connection::open(data.join("catalog.db"))?;
        conn.execute(
            "insert or replace into network_sample(at,mbps) values(?,?)",
            params![unix_seconds(), mbps],
        )?;
        conn.execute(
            "delete from network_sample where at<?",
            [unix_seconds() - 30 * 86_400],
        )?;
        if mbps < 1000 {
            conn.execute(
                "update scan_meta set last_started=?,root_reachable=1,last_error=? where id=1",
                params![
                    Utc::now().to_rfc3339(),
                    format!("link degraded to {mbps} Mbps")
                ],
            )?;
            return Ok(json!({"paused":true,"reason":"link_degraded","link_speed_mbps":mbps}));
        }
    }
    let conn = Connection::open(data.join("catalog.db"))?;
    let generation: i64 = conn.query_row(
        "update scan_meta set generation=generation+1,last_started=?,root_reachable=1,last_error='' where id=1 returning generation",
        [Utc::now().to_rfc3339()], |row| row.get(0))?;
    drop(conn);
    let mut indexed = Vec::new();
    let mut skipped = 0usize;
    let mut stabilizing = 0usize;
    let mut errors = Vec::new();
    let mut visibility_errors = Vec::new();
    let mut hot_errors = Vec::new();
    let mut warnings = Vec::new();
    for entry in WalkDir::new(root)
        .follow_links(false)
        .into_iter()
        .filter_entry(|e| {
            !matches!(
                e.file_name().to_string_lossy().as_ref(),
                "#recycle" | "@eaDir" | "20_需修正" | "90_历史版本" | "99_系统"
            )
        })
    {
        let entry = match entry {
            Ok(e) => e,
            Err(e) => {
                visibility_errors.push(e.to_string());
                errors.push(e.to_string());
                continue;
            }
        };
        let path = entry.path();
        if !entry.file_type().is_file() || !supported(path) {
            continue;
        }
        let metadata = match fs::metadata(path) {
            Ok(v) => v,
            Err(e) => {
                let message = format!("{}: {e}", path.display());
                visibility_errors.push(message.clone());
                errors.push(message);
                continue;
            }
        };
        let mtime_ns = metadata
            .modified()
            .ok()
            .and_then(|v| v.duration_since(UNIX_EPOCH).ok())
            .map(|v| v.as_nanos() as i64)
            .unwrap_or_default();
        let now = unix_seconds();
        let conn = Connection::open(data.join("catalog.db"))?;
        let existing = conn
            .query_row(
                "select size,mtime_ns,state,last_changed,sha256 from file_catalog where path=?",
                [path.to_string_lossy().as_ref()],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, i64>(3)?,
                        row.get::<_, String>(4)?,
                    ))
                },
            )
            .ok();
        let metadata_unchanged = existing.as_ref().is_some_and(|(size, mtime, _, _, _)| {
            *size == metadata.len() as i64 && *mtime == mtime_ns
        });
        if existing.is_none() {
            conn.execute(
                "insert into file_catalog(path,sha256,size,mtime_ns,state,last_seen_generation,last_changed) values(?,?,?,?,?,?,?)",
                params![path.to_string_lossy(), "", metadata.len() as i64, mtime_ns,
                    "stabilizing", generation, now],
            )?;
        } else if !metadata_unchanged {
            conn.execute(
                "update file_catalog set size=?,mtime_ns=?,state='stabilizing',error='',last_seen_generation=?,last_changed=?,missing_scans=0,missing_since=null where path=?",
                params![metadata.len() as i64, mtime_ns, generation, now, path.to_string_lossy()],
            )?;
        } else {
            conn.execute("update file_catalog set last_seen_generation=?,missing_scans=0,missing_since=null where path=?",
                params![generation, path.to_string_lossy()])?;
        }
        let ready = existing
            .as_ref()
            .is_some_and(|(_, _, state, _, _)| state == "ready")
            && metadata_unchanged;
        let stable_since = if metadata_unchanged {
            existing
                .as_ref()
                .map(|(_, _, _, changed, _)| *changed)
                .unwrap_or(now)
        } else {
            now
        };
        drop(conn);
        if ready {
            if let Some((_, _, _, _, sha)) = existing.as_ref() {
                if let Err(error) = cache_source(data, path, Some(sha)) {
                    hot_errors.push(format!("{}: {error:#}", path.display()));
                }
            }
            skipped += 1;
            continue;
        }
        if now.saturating_sub(stable_since) < 60 {
            stabilizing += 1;
            continue;
        }
        let (platform, store, source, authority) = infer_scope(root, path);
        let (known_sha, cached_path) = match cache_source(data, path, None) {
            Ok(value) => value,
            Err(e) => {
                errors.push(format!("{}: {e:#}", path.display()));
                continue;
            }
        };
        let conn = Connection::open(data.join("catalog.db"))?;
        let reusable = conn.query_row(
            "select rows,sheets,parquet_path from file_catalog where sha256=? and state='ready' and path<>? limit 1",
            params![known_sha, path.to_string_lossy()],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?, row.get::<_, String>(2)?)),
        ).ok().filter(|(_, _, parquet)| Path::new(parquet).is_file());
        if let Some((rows, sheets, parquet_path)) = reusable {
            conn.execute(
                "update file_catalog set sha256=?,platform=?,store_id=?,source=?,authority=?,state='ready',rows=?,sheets=?,parquet_path=?,error='',indexed_at=?,missing_scans=0,missing_since=null where path=?",
                params![known_sha, platform, store, source, authority, rows, sheets, parquet_path,
                    Utc::now().to_rfc3339(), path.to_string_lossy()],
            )?;
            skipped += 1;
            continue;
        }
        drop(conn);
        match index_file(
            data,
            path,
            &cached_path,
            &platform,
            &store,
            &source,
            &authority,
            Some(known_sha.clone()),
        ) {
            Ok(stats) => indexed.push(stats),
            Err(error)
                if matches!(
                    path.extension()
                        .and_then(|value| value.to_str())
                        .map(str::to_ascii_lowercase)
                        .as_deref(),
                    Some("xls" | "xlsb")
                ) =>
            {
                let message = format!("全文索引未生成，交给 Python 财务兼容解析：{error:#}");
                let connection = Connection::open(data.join("catalog.db"))?;
                connection.execute(
                    "update file_catalog set sha256=?,platform=?,store_id=?,source=?,authority=?,state='finance_only',error=?,indexed_at=?,missing_scans=0,missing_since=null where path=?",
                    params![known_sha, platform, store, source, authority, message,
                        Utc::now().to_rfc3339(), path.to_string_lossy()],
                )?;
                warnings.push(format!("{}: {message}", path.display()));
            }
            Err(e) => errors.push(format!("{}: {e:#}", path.display())),
        }
    }
    if let Err(error) = prune_hot_cache(data) {
        hot_errors.push(error.to_string());
    }
    let conn = Connection::open(data.join("catalog.db"))?;
    if visibility_errors.is_empty() {
        conn.execute("update file_catalog set missing_scans=missing_scans+1,missing_since=coalesce(missing_since,?) where last_seen_generation<>?",
            params![unix_seconds(), generation])?;
        conn.execute(
            "update scan_meta set last_completed=?,root_reachable=1,last_error=? where id=1",
            params![
                Utc::now().to_rfc3339(),
                errors.first().cloned().unwrap_or_default()
            ],
        )?;
    } else {
        conn.execute(
            "update scan_meta set root_reachable=0,last_error=? where id=1",
            [visibility_errors[0].clone()],
        )?;
    }
    Ok(
        json!({"generation": generation, "indexed": indexed, "skipped": skipped,
        "stabilizing": stabilizing, "errors": errors, "hot_errors": hot_errors,
        "warnings": warnings}),
    )
}

fn unix_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
}

#[cfg(windows)]
fn link_speed_mbps() -> Option<i64> {
    let script = "Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | Sort-Object LinkSpeed -Descending | Select-Object -First 1 -ExpandProperty LinkSpeed";
    let output = ProcessCommand::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command", script])
        .output()
        .ok()?;
    let label = String::from_utf8_lossy(&output.stdout)
        .trim()
        .to_ascii_lowercase();
    let number = label.split_whitespace().next()?.parse::<f64>().ok()?;
    if label.contains("gbps") {
        Some((number * 1000.0) as i64)
    } else if label.contains("mbps") {
        Some(number as i64)
    } else {
        None
    }
}

#[cfg(not(windows))]
fn link_speed_mbps() -> Option<i64> {
    None
}

fn search_index(
    data: &Path,
    query_text: &str,
    limit: usize,
    store_id: Option<&str>,
    platform: Option<&str>,
    source: Option<&str>,
) -> Result<Vec<SearchHit>> {
    let runtime = open_search_runtime(data)?;
    search_runtime(
        data, &runtime, query_text, limit, store_id, platform, source,
    )
}

fn search_runtime(
    data: &Path,
    runtime: &SearchRuntime,
    query_text: &str,
    limit: usize,
    store_id: Option<&str>,
    platform: Option<&str>,
    source: Option<&str>,
) -> Result<Vec<SearchHit>> {
    let fields = runtime.fields;
    let searcher = runtime.reader.searcher();
    let query: Box<dyn TantivyQuery> = if is_identifier(query_text) {
        Box::new(TermQuery::new(
            Term::from_field_text(fields.identifier, &query_text.to_ascii_lowercase()),
            IndexRecordOption::Basic,
        ))
    } else {
        let mut parser = QueryParser::for_index(&runtime.index, vec![fields.all_text]);
        parser.set_conjunction_by_default();
        parser.parse_query(query_text)?
    };
    let candidate_limit = limit.saturating_mul(20).clamp(limit, 5000);
    let top = searcher.search(
        &query,
        &TopDocs::with_limit(candidate_limit).order_by_score(),
    )?;
    let needle = query_text.to_lowercase();
    let mut out = Vec::new();
    for (score, address) in top {
        let document = searcher.doc::<TantivyDocument>(address)?;
        let cells_json = document
            .get_first(fields.cells_json)
            .and_then(|v| v.as_str())
            .unwrap_or("[]");
        let cells: Vec<String> = serde_json::from_str(cells_json).unwrap_or_default();
        let matches = cells
            .iter()
            .enumerate()
            .filter_map(|(column_index, value)| {
                value.to_lowercase().contains(&needle).then(|| CellMatch {
                    column_index,
                    value: value.clone(),
                })
            })
            .collect::<Vec<_>>();
        if matches.is_empty() {
            continue;
        }
        let text = cells
            .iter()
            .filter(|v| !v.is_empty())
            .cloned()
            .collect::<Vec<_>>()
            .join("\t");
        let file_sha = get_text(&document, fields.file_sha);
        let scope = catalog_scope(data, &file_sha, store_id, platform, source)?;
        let Some((path, platform, store_id, source, authority)) = scope else {
            continue;
        };
        out.push(SearchHit {
            score,
            file_sha,
            path,
            sheet: get_text(&document, fields.sheet),
            row_no: document
                .get_first(fields.row_no)
                .and_then(|v| v.as_u64())
                .unwrap_or_default(),
            platform,
            store_id,
            source,
            authority,
            matches,
            snippet: make_snippet(&text, query_text),
        });
        if out.len() >= limit {
            break;
        }
    }
    Ok(out)
}

fn preview_runtime(data: &Path, runtime: &SearchRuntime, args: &PreviewArgs) -> Result<Value> {
    let fields = runtime.fields;
    let mut clauses: Vec<(Occur, Box<dyn TantivyQuery>)> = vec![(
        Occur::Must,
        Box::new(TermQuery::new(
            Term::from_field_text(fields.file_sha, &args.sha),
            IndexRecordOption::Basic,
        )),
    )];
    if let Some(sheet) = args.sheet.as_deref().filter(|value| !value.is_empty()) {
        clauses.push((
            Occur::Must,
            Box::new(TermQuery::new(
                Term::from_field_text(fields.sheet, sheet),
                IndexRecordOption::Basic,
            )),
        ));
    }
    let query = BooleanQuery::new(clauses);
    let offset = args.offset.unwrap_or(0).min(1_000_000);
    let limit = args.limit.unwrap_or(30).clamp(1, 200);
    let searcher = runtime.reader.searcher();
    let top = searcher.search(
        &query,
        &TopDocs::with_limit(limit)
            .and_offset(offset)
            .order_by_u64_field("row_no", Order::Asc),
    )?;
    let mut rows = Vec::with_capacity(top.len());
    for (_sort_value, address) in top {
        let document = searcher.doc::<TantivyDocument>(address)?;
        let cells_json = document
            .get_first(fields.cells_json)
            .and_then(|value| value.as_str())
            .unwrap_or("[]");
        rows.push(json!({
            "sheet": get_text(&document, fields.sheet),
            "row_no": document.get_first(fields.row_no).and_then(|value| value.as_u64()).unwrap_or(0),
            "cells": serde_json::from_str::<Value>(cells_json).unwrap_or_else(|_| json!([])),
        }));
    }
    let connection = Connection::open(data.join("catalog.db"))?;
    let metadata = connection.query_row(
        "select path,platform,store_id,source,authority,rows from file_catalog where sha256=? and state='ready' order by case authority when 'calculation' then 0 else 1 end,path limit 1",
        [&args.sha],
        |row| Ok(json!({
            "path": row.get::<_, String>(0)?, "platform": row.get::<_, String>(1)?,
            "store_id": row.get::<_, String>(2)?, "source": row.get::<_, String>(3)?,
            "authority": row.get::<_, String>(4)?, "total_rows": row.get::<_, i64>(5)?,
        })),
    ).unwrap_or_else(|_| json!({}));
    Ok(json!({"sha256":args.sha,"offset":offset,"limit":limit,"metadata":metadata,"rows":rows}))
}

fn catalog_scope(
    data: &Path,
    sha: &str,
    store_id: Option<&str>,
    platform: Option<&str>,
    source: Option<&str>,
) -> Result<Option<(String, String, String, String, String)>> {
    let conn = Connection::open(data.join("catalog.db"))?;
    let mut sql = String::from(
        "select path,platform,store_id,source,authority from file_catalog where sha256=? and state='ready' and missing_scans=0",
    );
    let mut values = vec![sha.to_string()];
    for (column, value) in [
        ("store_id", store_id),
        ("platform", platform),
        ("source", source),
    ] {
        if let Some(value) = value.filter(|value| !value.is_empty()) {
            sql.push_str(&format!(" and {column}=?"));
            values.push(value.to_string());
        }
    }
    sql.push_str(" order by case authority when 'calculation' then 0 else 1 end,path limit 1");
    let mut statement = conn.prepare(&sql)?;
    let mut rows = statement.query(rusqlite::params_from_iter(values))?;
    if let Some(row) = rows.next()? {
        Ok(Some((
            row.get(0)?,
            row.get(1)?,
            row.get(2)?,
            row.get(3)?,
            row.get(4)?,
        )))
    } else {
        Ok(None)
    }
}

fn get_text(doc: &TantivyDocument, field: tantivy::schema::Field) -> String {
    doc.get_first(field)
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string()
}

fn make_snippet(text: &str, query: &str) -> String {
    let lower = text.to_lowercase();
    let needle = query.to_lowercase();
    if let Some(start) = lower.find(&needle) {
        let from = start.saturating_sub(60);
        let to = (start + query.len() + 120).min(text.len());
        text.get(from..to).unwrap_or(text).to_string()
    } else {
        text.chars().take(180).collect()
    }
}

async fn serve(root: PathBuf, data: PathBuf, bind: SocketAddr) -> Result<()> {
    init_data_dir(&data)?;
    let search = Arc::new(open_search_runtime(&data)?);
    let state = AppState { root, data, search };
    let scan_state = state.clone();
    tokio::spawn(async move {
        let mut ticker = time::interval(Duration::from_secs(30));
        loop {
            ticker.tick().await;
            let root = scan_state.root.clone();
            let data = scan_state.data.clone();
            let result = tokio::task::spawn_blocking(move || scan_once(&root, &data)).await;
            if result.is_ok() {
                let _ = scan_state.search.reader.reload();
            }
        }
    });
    let app = Router::new()
        .route("/health", get(health_handler))
        .route("/status", get(status_handler))
        .route("/files", get(files_handler))
        .route("/jobs", get(jobs_handler))
        .route("/errors", get(errors_handler))
        .route("/storage", get(storage_handler))
        .route("/search", get(search_handler))
        .route("/preview", get(preview_handler))
        .with_state(state);
    axum::serve(tokio::net::TcpListener::bind(bind).await?, app).await?;
    Ok(())
}

async fn health_handler(State(state): State<AppState>) -> Json<Value> {
    Json(
        json!({"ok": state.root.exists() && state.data.join("catalog.db").exists(), "root_reachable": state.root.exists()}),
    )
}

async fn status_handler(State(state): State<AppState>) -> Json<Value> {
    let value = tokio::task::spawn_blocking(move || -> Result<Value> {
        let conn = Connection::open(state.data.join("catalog.db"))?; let mut counts = BTreeMap::new();
        let mut stmt = conn.prepare("select state,count(*) from file_catalog group by state")?;
        for row in stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))? { let (s, c): (String, i64) = row?; counts.insert(s, c); }
        let meta: (i64, String, String, i64, String) = conn.query_row(
            "select generation,last_started,last_completed,root_reachable,last_error from scan_meta where id=1", [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?;
        let link_speed_mbps: Option<i64> = conn.query_row(
            "select mbps from network_sample order by at desc limit 1", [], |row| row.get(0),
        ).ok();
        Ok(json!({"generation":meta.0,"last_started":meta.1,"last_completed":meta.2,
            "root_reachable":meta.3!=0,"last_error":meta.4,"files":counts,
            "link_speed_mbps":link_speed_mbps,
            "link_degraded":link_speed_mbps.is_some_and(|value| value<1000)}))
    }).await.ok().and_then(Result::ok).unwrap_or_else(|| json!({"error":"catalog unavailable"}));
    Json(value)
}

async fn search_handler(
    State(state): State<AppState>,
    Query(args): Query<SearchArgs>,
) -> Json<Value> {
    let value = tokio::task::spawn_blocking(move || {
        search_runtime(
            &state.data,
            &state.search,
            &args.q,
            args.limit.unwrap_or(50).min(1000),
            args.store_id.as_deref(),
            args.platform.as_deref(),
            args.source.as_deref(),
        )
    })
    .await
    .ok()
    .and_then(Result::ok)
    .map(|hits| json!({"hits":hits}))
    .unwrap_or_else(|| json!({"error":"search failed","hits":[]}));
    Json(value)
}

async fn preview_handler(
    State(state): State<AppState>,
    Query(args): Query<PreviewArgs>,
) -> Json<Value> {
    let value =
        tokio::task::spawn_blocking(move || preview_runtime(&state.data, &state.search, &args))
            .await
            .ok()
            .and_then(Result::ok)
            .unwrap_or_else(|| json!({"error":"preview failed","rows":[]}));
    Json(value)
}

fn catalog_rows(data: &Path, sql: &str) -> Result<Value> {
    let conn = Connection::open(data.join("catalog.db"))?;
    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map([], |row| Ok(json!({
        "path": row.get::<_, String>(0)?, "sha256": row.get::<_, String>(1)?,
        "platform": row.get::<_, String>(2)?, "store_id": row.get::<_, String>(3)?,
        "source": row.get::<_, String>(4)?, "authority": row.get::<_, String>(5)?,
        "state": row.get::<_, String>(6)?, "rows": row.get::<_, i64>(7)?,
        "error": row.get::<_, String>(8)?, "indexed_at": row.get::<_, String>(9)?,
        "missing_scans": row.get::<_, i64>(10)?, "missing_since": row.get::<_, Option<i64>>(11)?,
    })))?.collect::<std::result::Result<Vec<_>, _>>()?;
    Ok(json!({"files": rows}))
}

async fn files_handler(State(state): State<AppState>) -> Json<Value> {
    let value = tokio::task::spawn_blocking(move || catalog_rows(&state.data,
        "select path,sha256,platform,store_id,source,authority,state,rows,error,indexed_at,missing_scans,missing_since from file_catalog order by indexed_at desc,path limit 1000"))
        .await.ok().and_then(Result::ok).unwrap_or_else(|| json!({"error":"catalog unavailable","files":[]}));
    Json(value)
}

async fn errors_handler(State(state): State<AppState>) -> Json<Value> {
    let value = tokio::task::spawn_blocking(move || catalog_rows(&state.data,
        "select path,sha256,platform,store_id,source,authority,state,rows,error,indexed_at,missing_scans,missing_since from file_catalog where error<>'' or state in ('error','quarantined') order by indexed_at desc,path limit 1000"))
        .await.ok().and_then(Result::ok).unwrap_or_else(|| json!({"error":"catalog unavailable","files":[]}));
    Json(value)
}

async fn jobs_handler(State(state): State<AppState>) -> Json<Value> {
    let value = tokio::task::spawn_blocking(move || -> Result<Value> {
        let conn = Connection::open(state.data.join("catalog.db"))?;
        let mut stmt = conn.prepare("select id,kind,path,state,created_at,started_at,finished_at,error from jobs order by id desc limit 1000")?;
        let rows = stmt.query_map([], |row| Ok(json!({"id":row.get::<_,i64>(0)?,
            "kind":row.get::<_,String>(1)?,"path":row.get::<_,String>(2)?,
            "state":row.get::<_,String>(3)?,"created_at":row.get::<_,String>(4)?,
            "started_at":row.get::<_,String>(5)?,"finished_at":row.get::<_,String>(6)?,
            "error":row.get::<_,String>(7)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        Ok(json!({"jobs":rows}))
    }).await.ok().and_then(Result::ok).unwrap_or_else(|| json!({"error":"catalog unavailable","jobs":[]}));
    Json(value)
}

fn directory_bytes(path: &Path) -> u64 {
    WalkDir::new(path)
        .follow_links(false)
        .into_iter()
        .filter_map(Result::ok)
        .filter(|entry| entry.file_type().is_file())
        .filter_map(|entry| entry.metadata().ok().map(|meta| meta.len()))
        .sum()
}

async fn storage_handler(State(state): State<AppState>) -> Json<Value> {
    let value = tokio::task::spawn_blocking(move || {
        let connection = Connection::open(state.data.join("catalog.db")).ok();
        let hot_entries = connection.as_ref().and_then(|conn| conn.query_row(
            "select count(*) from hot_cache", [], |row| row.get::<_, i64>(0),
        ).ok()).unwrap_or(0);
        json!({
            "catalog_bytes": fs::metadata(state.data.join("catalog.db")).map(|v|v.len()).unwrap_or(0),
            "tantivy_bytes": directory_bytes(&state.data.join(INDEX_DIR)),
            "legacy_tantivy_bytes": directory_bytes(&state.data.join("tantivy")),
            "parquet_bytes": directory_bytes(&state.data.join("parquet")),
            "hot_bytes": directory_bytes(&state.data.join("hot")),
            "hot_entries": hot_entries,
            "hot_limit_bytes": HOT_LIMIT_BYTES,
            "hot_max_age_days": 30,
        })
    }).await.unwrap_or_else(|_| json!({"error":"storage unavailable"}));
    Json(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn scope_uses_directory_store_id() {
        let root = Path::new(r"X:\台账系统\10_已接收");
        let path = root.join(r"拼多多\吴鹏-PDDzlvoey [pdd_zlvoey]\订单明细\订单.csv");
        let got = infer_scope(root, &path);
        assert_eq!(
            (&got.0[..], &got.1[..], &got.2[..], &got.3[..]),
            ("拼多多", "pdd_zlvoey", "订单明细", "calculation")
        );
    }
    #[test]
    fn temp_files_are_ignored() {
        assert!(!supported(Path::new("~$订单.xlsx")));
        assert!(!supported(Path::new("订单.xlsx.part")));
        assert!(supported(Path::new("订单.xlsx")));
        assert!(supported(Path::new("历史订单.xls")));
        assert!(supported(Path::new("历史订单.xlsb")));
    }

    #[test]
    fn gbk_csv_is_decoded_without_replacement() {
        let (encoded, _encoding, had_errors) = GBK.encode("拼多多,商户订单号\n收入,260620-1\n");
        assert!(!had_errors);
        let decoded = decode_csv_bytes(&encoded).unwrap();
        assert!(decoded.contains("商户订单号"));
        assert!(!decoded.contains('\u{fffd}'));
    }

    #[test]
    fn identifiers_include_embedded_order_tokens_but_not_amounts() {
        let cells = vec![
            "18122019:5119142221120002016".to_string(),
            "HSC05548".to_string(),
            "188.36".to_string(),
        ];
        let got = identifier_tokens(&cells);
        assert!(got.contains(&"5119142221120002016".to_string()));
        assert!(got.contains(&"hsc05548".to_string()));
        assert!(!got.contains(&"188".to_string()));
    }
}
