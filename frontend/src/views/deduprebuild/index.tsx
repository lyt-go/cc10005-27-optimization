import { useCallback, useEffect, useMemo, useState } from 'react'

import { fetchJson, request } from '@/api/client'

const ENDPOINT = '/api/deduprebuild'
const columns = [
  "批次编号",
  "去噪规则",
  "去重引擎",
  "去重字段组",
  "变化字段",
  "影响判定",
  "断点窗口",
  "去重结果"
]
const actions = [
  "识别影响范围",
  "启动局部重建",
  "中断重建",
  "断点续建",
  "完成重建",
  "冲突裁决",
  "转入历史"
]
const detailFields = [
  "清洗版本",
  "命中字段",
  "字段完整度",
  "窗口总数",
  "已完成窗口",
  "复用窗口",
  "窗口边界",
  "相似度阈值",
  "复用条件",
  "哈希指纹",
  "裁决说明",
  "确认时间"
]
const windowColumns = ["窗口编号", "窗口边界", "清洗版本", "哈希指纹", "旧哈希指纹", "去重结果", "记录数"]

type Row = Record<string, string | number | null>
type Detail = Row & { windows?: Row[] }
type PlanEngine = {
  去重引擎: string
  去重字段组: string
  命中字段: string
  字段完整度: string
  影响判定: string
  处置: string
}
type Plan = {
  去噪规则: string
  清洗版本: string
  变化字段: string
  复用条件: string
  受影响引擎数: number
  复用引擎数: number
  新建批次: string[]
  引擎识别结果: PlanEngine[]
}

export default function DeduprebuildView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<Detail | null>(null)
  const [plan, setPlan] = useState<Plan | null>(null)
  const [planForm, setPlanForm] = useState({ 规则编号: 'DATA-0002', 变化字段: '标题、正文', 目标清洗版本: 'v5' })
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('重建批次列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '去重索引重建列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const stats = useMemo(() => ([
    { label: '重建批次总数', value: total },
    { label: '受影响批次', value: rows.filter((row) => row['影响判定'] === '受影响').length },
    { label: '复用窗口', value: rows.reduce((sum, row) => sum + Number(row['复用窗口'] ?? 0), 0) },
    { label: '转历史批次', value: rows.filter((row) => row.status === '已转历史').length },
  ]), [rows, total])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message || '去重索引重建动作未生效，请稍后重试')
      }
      setNotice(payload.message ?? '')
      await reload()
      if (detail && detail.id === row.id) {
        await openDetail(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '去重索引重建操作失败')
    }
  }

  async function openDetail(row: Row) {
    setErrorMessage('')
    try {
      const payload = await fetchJson<Detail>(`${ENDPOINT}/${row.id}`)
      setDetail(payload)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重建批次详情读取失败')
    }
  }

  async function submitPlan() {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/plan`, {
        method: 'POST',
        body: JSON.stringify({ values: planForm }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message || '影响识别失败')
      }
      setPlan(payload.plan ?? null)
      setNotice(payload.message ?? '')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '影响识别失败')
    }
  }

  return (
    <section className="page" data-module="deduprebuild">
      <header className="page-head">
        <div>
          <h2>去重索引重建</h2>
          <p className="page-desc">字段清洗版本变化后只重建命中变化字段的去重引擎与滑动窗口，稳定去重结果继续复用；中断后从最后一个完整窗口续建，冲突窗口按字段完整度与确认顺序裁决。</p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出去重索引重建清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void submitPlan() }}>
        <label className="filter-item">
          <span>去噪规则</span>
          <input
            value={planForm.规则编号}
            placeholder="如 DATA-0002"
            onChange={(event) => setPlanForm((prev) => ({ ...prev, 规则编号: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>变化字段</span>
          <input
            value={planForm.变化字段}
            placeholder="如 标题、正文"
            onChange={(event) => setPlanForm((prev) => ({ ...prev, 变化字段: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>目标清洗版本</span>
          <input
            value={planForm.目标清洗版本}
            placeholder="如 v5"
            onChange={(event) => setPlanForm((prev) => ({ ...prev, 目标清洗版本: event.target.value }))}
          />
        </label>
        <button className="btn primary" type="submit">识别受影响引擎</button>
      </form>

      {plan && (
        <section className="detail-panel">
          <header className="detail-head">
            <strong>影响识别结果：{plan.去噪规则}（{plan.清洗版本}）</strong>
            <span className="stat-label">变化字段 {plan.变化字段} · 受影响 {plan.受影响引擎数} · 复用 {plan.复用引擎数} · 复用条件：{plan.复用条件}</span>
            <button className="link" type="button" onClick={() => setPlan(null)}>收起</button>
          </header>
          <table className="data-table">
            <thead>
              <tr>
                <th>去重引擎</th><th>去重字段组</th><th>命中字段</th><th>字段完整度</th><th>影响判定</th><th>处置</th>
              </tr>
            </thead>
            <tbody>
              {plan.引擎识别结果.map((engine) => (
                <tr key={engine.去重引擎}>
                  <td>{engine.去重引擎}</td>
                  <td>{engine.去重字段组}</td>
                  <td>{engine.命中字段}</td>
                  <td>{engine.字段完整度}</td>
                  <td>{engine.影响判定}</td>
                  <td>{engine.处置}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openDetail(row)}>详情</button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无去重索引重建数据，可先用上方表单识别受影响引擎</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <section className="detail-panel">
          <header className="detail-head">
            <strong>重建批次详情：{String(detail['批次编号'])}（{String(detail['去噪规则'])} → {String(detail['去重引擎'])}）</strong>
            <span className="stat-label">去重结果：{String(detail['去重结果'] ?? '—')}</span>
            <button className="link" type="button" onClick={() => setDetail(null)}>收起</button>
          </header>
          <div className="detail-grid">
            {detailFields.map((field) => (
              <div key={field} className="detail-item">
                <span className="stat-label">{field}</span>
                <span>{detail[field] ?? '—'}</span>
              </div>
            ))}
          </div>
          <table className="data-table">
            <thead>
              <tr>
                {windowColumns.map((column) => <th key={column}>{column}</th>)}
              </tr>
            </thead>
            <tbody>
              {(detail.windows ?? []).map((window) => (
                <tr key={String(window['窗口编号'])}>
                  {windowColumns.map((column) => <td key={column}>{window[column] ?? '—'}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条去重索引重建记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
