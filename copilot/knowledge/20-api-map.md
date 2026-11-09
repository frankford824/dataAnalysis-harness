# 接口地图（GET 只读；耗时为生产实测）
基址 http://127.0.0.1:8000，单 worker，慢接口会挤占其他请求。共 188 条路由，api_index(关键词) 可查。
python 里 `get(path, **query)` 返回 JSON（超时 300s）；ledger_get 90s 超时且截断 6000 字。

## 禁用/慢
- /api/overview、/api/automation/months/{p}：极慢，禁用。
- /api/stores/{id}：>60s，店铺信息用 /api/navigation。
- /api/stores/{id}/periods/{p}：~35s、160KB，只在需要细项时调，用 python 取字段。
- /api/automation/inbox?period=：~30s。/api/fees：8s 171KB。/api/commission?period=（旧版）：8s。

## 快接口
- /api/version 0.2s {version, model_revision, source_code_sha, source_code_status}
- /api/health 1.4s {ok, workspace_generation, ingest{ingest_mode nas, nas_reachable, upload_path}, index{}, order_feed{consumed_seq, source_latest_seq, last_success, last_error, health}}
- /api/navigation 3s {platforms[{id,name}], periods[], default_period, stores[{id,name,platform,archived,aliases,commission_base,latest_period,latest_state,file_count}]}
- /api/trend?store_id=&platform=&periods=12 5–6s {periods[], stores{月:店数}, rows[{id,name,level,is_total,headline,cells{月:{value,stores}}}], scope}。值为损益表口径（成本负）；只加已出数的店，cells.stores 是加进来的店数。
- /api/operations/month?period= 2–8s。参数 level=store|team|person|product|order, platform, store_id, team_id, person_id, product_id, managed, offset, limit(默认50), sort_key(默认 profit_after_labor), sort_direction。返回 {period, run_refs[{store_id,run_id,state,stale,evidence_complete}], is_final, metrics[{key,label}], summary{…}, rows[{id,name,store_id,platform,person_id,team,product_id,order_count,metrics{…},rank,mom,yoy}], issues[文字], total, offset, limit}。rows 分页！找某店用 store_id 过滤。
- /api/gaps?period=&store_id= 5–8s {cells[{store_id,store,platform,period,state,count,worst,gaps[{kind blocking|missing|dropped|unlinked|coverage, severity, title, detail, node, metric, source, amount}]}]}。全店 190KB→python 汇总 title 计数。
- /api/runs/{run_id}/drill/{node_id}?limit=&offset=&subject=&file=&q=&only= 0.8s {name,value,total,rows,uncounted{rows,amount},by_subject[{subject,count,amount}],by_file[{file,sheet,count,amount}],sample[{order_id,subject,amount,contribution,counted,linked,file_name,row_no}]}。node_id 用 n_*/g_*。
- /api/runs/{run}/coverage-gaps?q=订单号 0.7s {total,line_revision,category_counts[{category,label,count}],items[{order_id,sub_order_id,product_ids,order_state,pending_cost_skus,category_label,coverage_key,context_sha,editable,line_revision,posted_goods_cost,manual_amount,coverage_reason}]}；.xlsx 导出。
  逐单补录成本：run 用该店月最新 run_id；按订单号查出那一行（editable 为 true 才能逐单补），确认卡动作 POST /api/stores/{id}/periods/{p}/cost-lines body {run_id, coverage_key, context_sha, amount, reason, expected_line_revision=该行 line_revision}。amount 只填还没计入的部分（posted_goods_cost 已计的不要重复），赠品填 0。cost-lines/batch-preview 要上传 Excel 文件，对话里不用它。台账在确认时会自动换到最新一次计算，同一单内容没变就照常写入。
- 人工成本调整（改任意一行或定商品规则，不绑定 run）：确认卡动作 POST /api/cost-adjustments body {items:[{kind:"line", store_id, period:"2026-08", order_id, sub_order_id, sku, unit_cost 或 total_cost, reason}]}。
  order_id 填平台订单号，sub_order_id 填平台子订单号，sku 填商品编码；total_cost 要求只对上一行，清 0 填 unit_cost 0。
  规则 {kind:"rule", store_id:店铺或"*", sku, with_sku(可选，同一平台订单里有这个商品时才生效), unit_cost, reason}。后写的调整优先。
  保存后店铺自动重算，明细说明写「人工调整 #编号：…」；没对上的会在成本说明里列出编号。GET /api/cost-adjustments?store_id= 看当前有效清单；
  撤销 POST /api/cost-adjustments/revoke {ids:[编号], reason}。
- /api/runs/{run}/fees.csv、/api/runs/{run}/allocation.csv：费用/分摊明细下载。
- /api/search?q=&limit= 0.9s（全文索引）{total,amount,by_store,hits[{subject,metric,amount,store,store_id,period,source,file,path,sheet,row_no,snippet}]}。
- /api/recompute/progress 0.7s {items[{store_id,store,state,phase,percent,seconds}]}
- /api/stores/{id}/pricing-status?period= 0.7s {run_id,calculated_at,state,percent,message}
- /api/automation/workers/status 0.1s {worker_alive,worker_healthy,processing_by_stage,workers[{id,status,heartbeat_age_seconds}]}
- /api/automation/rules 0.4s {enabled, requires_human_grant, rules[]}；/api/automation/agent-authority?period=
- /api/order-feed/status 0.1s（订单台 8001 同步）；/api/operations/sentinel/status 0.1s
- /api/commission-v2/labor?period= 0.7s {amount(null=未填写),locked,closed_stores,basis_total,rows[{store_id,store,closed,sales,share,amount,frozen_amount}]}
- /api/commission-v2/payout?store_id=&period= 0.6s {stores[{store_id,state,finance_run,total,base_total,base_name,on_loss,unassigned_orders,pricing_pending_count,amount_complete}],people[{person_id,person,amount,stores}],total(null=不完整),calculated_total}
- /api/commission-v2/status 0.4s；/reports/query、/settlements、/payout-confirmations、/org/tree、/people、/settings
- /api/freight-review?period=&store_id= 3s {summary{store_count,closed_count,shown_amount,trial_amount,change},stores[{store,shown_amount,shown_reason,trial_amount,trial_status}],audit_status}
- /api/index/errors、/api/index/files：源文件解析状态。

## 店月详情 /api/stores/{id}/periods/{p} 主要字段
state, stale, run_id, history[], can_close, pricing_pending_count, cost_coverage{coverage,threshold,passed,covered,expected,uncovered}, statement[{id,name,value,available,unavailable_reason,missing_sources}], findings[{name,passed,blocking,message}], sources[{id,name,arrived,reason}], missing_sources, quality[{name,hit_rate,coverage}], unclassified[{label,count,amount}], unlinked_buckets[{label,count,amount,why}], commission{total,people[],amount_complete}, file_errors[{file,sheet,reason}], close_verification{allowed,issues[{code,message,hard}]}, calculation_inputs{metric_totals}.

## 写接口（必须 propose_change 让人确认；ledger_preview 只能调 *preview*）
POST /api/stores/{id}/recompute（整店重算）；/api/stores/{id}/periods/{p}/close|reopen|cost-lines|cost-lines/batch-preview→batch-apply；/api/automation/close-batches/preview→/confirm(proposal_id,proposal_sha,input_sha)；/api/automation/decisions/preview→/apply；/api/fees/preview→/apply；/api/commission-v2/labor(period,name,amount,revision)；cost-lines 保存。
自动化写接口要求人工身份（Agent 不能代替人工确认财务决定）。
