import axios from 'axios'
import addonData from '/src/common'
import React, { useState, useEffect, useCallback } from 'react'
import { Panel, ScrollPanel, Button } from '@ynput/ayon-react-components'

import styled from 'styled-components'

const IssuesPanel = styled(Panel)`
  min-width: 650px;
  max-width: 900px;
  min-height: 280px;
  max-height: 85%;
`

const Toolbar = styled.div`
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
  flex-wrap: wrap;
`

const Err = styled.div`
  color: #c62828;
  margin-bottom: 8px;
  font-size: 13px;
`

const Table = styled.table`
  border-collapse: collapse;
  width: 100%;
  font-size: 13px;

  thead {
    border-bottom: 1px solid #ccc;
  }

  th,
  td {
    padding: 6px 8px;
    text-align: left;
    vertical-align: top;
  }

  th {
    font-weight: bold;
  }

  tr:nth-child(even) {
    background: rgba(0, 0, 0, 0.03);
  }
`

const HeadlineCell = styled.td`
  max-width: 420px;
  word-break: break-word;
`

const DetailBlock = styled.pre`
  margin: 8px 0 0 0;
  padding: 8px;
  background: #f5f5f5;
  border-radius: 4px;
  font-size: 11px;
  overflow-x: auto;
  max-height: 240px;
`

const phaseLabel = (summary) => {
  if (!summary || typeof summary !== 'object') return '—'
  return summary.phase || summary.entityType || '—'
}

const SyncIssuesPanel = () => {
  const [issues, setIssues] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [expanded, setExpanded] = useState({})

  const load = useCallback(() => {
    setLoading(true)
    setError(null)
    axios
      .get(`${addonData.baseUrl}/processor/sync-issues`, {
        params: { limit: 100, dedupe: true },
      })
      .then((res) => {
        setIssues(res.data?.issues || [])
      })
      .catch((e) => {
        const msg =
          e.response?.data?.detail ||
          e.response?.data?.message ||
          e.message ||
          'Failed to load sync issues'
        setError(String(msg))
        setIssues([])
      })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const toggle = (id) => {
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }))
  }

  return (
    <IssuesPanel>
      <h1 style={{ fontSize: 18, margin: '0 0 12px 0' }}>Sync issues</h1>
      <p style={{ fontSize: 13, color: '#555', margin: '0 0 12px 0' }}>
        Recent Kitsu→AYON push failures and partial-sync summaries (from stored
        events). Duplicates per entity are collapsed when possible.
      </p>
      <Toolbar>
        <Button onClick={load} disabled={loading}>
          {loading ? 'Loading…' : 'Refresh'}
        </Button>
      </Toolbar>
      {error ? <Err>{error}</Err> : null}
      <ScrollPanel style={{ maxHeight: '60vh' }}>
        {issues.length === 0 && !loading ? (
          <p style={{ fontSize: 13 }}>No sync issues recorded.</p>
        ) : (
          <Table>
            <thead>
              <tr>
                <th>When</th>
                <th>Project</th>
                <th>Phase</th>
                <th>Headline</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {issues.map((row) => {
                const id = row.id || `${row.created_at}-${row.description}`
                const open = !!expanded[id]
                return (
                  <React.Fragment key={id}>
                    <tr>
                      <td>{row.created_at || '—'}</td>
                      <td>{row.project_name || '—'}</td>
                      <td>{phaseLabel(row.summary)}</td>
                      <HeadlineCell>
                        {row.description || row.topic || '—'}
                      </HeadlineCell>
                      <td>
                        <Button onClick={() => toggle(id)} style={{ fontSize: 12 }}>
                          {open ? 'Hide' : 'Details'}
                        </Button>
                      </td>
                    </tr>
                    {open ? (
                      <tr>
                        <td colSpan={5}>
                          <DetailBlock>
                            {JSON.stringify(
                              {
                                topic: row.topic,
                                status: row.status,
                                summary: row.summary,
                                payload: row.payload,
                              },
                              null,
                              2
                            )}
                          </DetailBlock>
                        </td>
                      </tr>
                    ) : null}
                  </React.Fragment>
                )
              })}
            </tbody>
          </Table>
        )}
      </ScrollPanel>
    </IssuesPanel>
  )
}

export default SyncIssuesPanel
