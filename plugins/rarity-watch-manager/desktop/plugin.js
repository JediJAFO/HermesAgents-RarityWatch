import { Button, Input, PANES_AREA, PALETTE_AREA, STATUSBAR_AREAS, host, atom, useValue } from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const $result = atom({ kind: 'idle', title: 'Ready', detail: 'List Saved reads disk only. Add performs live marketplace calls.' })
let pluginCtx = null

function formatResult(data) {
  if (Array.isArray(data?.watches)) {
    const rows = data.watches.map(x => `${x.enabled ? 'ON ' : 'OFF'} ${String(x.rarity).padEnd(9)} ${x.name}  ${x.contract}`)
    return [`${data.count} saved watch(es)`, ...rows].join('\n')
  }
  return JSON.stringify(data, null, 2)
}

async function runAction(body) {
  const label = body.action === 'list' ? 'List Saved' : `${body.action[0].toUpperCase()}${body.action.slice(1)} ${body.rarity}`
  $result.set({ kind: 'working', title: label, detail: body.action === 'add' ? 'Working — live metadata and marketplace onboarding calls are running…' : 'Working — saved state only; no live calls…' })
  try {
    const data = await pluginCtx.rest('/execute', { method: 'POST', body, timeoutMs: body.action === 'add' ? 430000 : 15000 })
    $result.set({ kind: 'complete', title: `${label} complete`, detail: formatResult(data) })
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    $result.set({ kind: 'error', title: `${label} failed`, detail })
    host.notify({ kind: 'error', message: `Rarity Watch Manager: ${detail}` })
  }
}

function Result() {
  const result = useValue($result)
  const tone = result.kind === 'error' ? 'text-(--ui-danger)' : result.kind === 'working' ? 'text-(--ui-accent)' : 'text-(--ui-text-secondary)'
  return jsxs('section', { className: 'grid gap-1 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-3', 'aria-live': 'polite', children: [
    jsx('div', { className: 'text-[11px] font-bold tracking-[0.08em] text-(--ui-text-tertiary) uppercase', children: 'Result' }),
    jsx('div', { className: `text-sm font-semibold ${tone}`, children: result.title }),
    jsx('pre', { className: 'm-0 max-h-48 overflow-auto whitespace-pre font-mono text-xs leading-5 tabular-nums text-(--ui-text-secondary)', children: result.detail })
  ]})
}

function Panel() {
  const [contract, setContract] = useState('')
  const [rarity, setRarity] = useState('Exotic')
  const [display, setDisplay] = useState('')
  const [category, setCategory] = useState('')
  const request = action => ({ action, contract: contract.trim(), rarity, ...(display.trim() ? {display:display.trim()} : {}), ...(category.trim() ? {category:category.trim()} : {}) })
  return jsxs('main', { className: 'flex h-full min-h-0 flex-col gap-3 overflow-auto p-3 text-sm', children: [
    jsxs('header', { className: 'grid gap-1', children: [
      jsx('h2', { className: 'm-0 text-sm font-semibold', children: 'Exotic / Legendary Watch Manager' }),
      jsx('p', { className: 'm-0 text-xs text-(--ui-text-tertiary)', children: 'Zero LLM tokens. Add makes live metadata + marketplace calls and seeds existing inventory without an alert. Other actions are saved-state only.' })
    ]}),
    jsxs('section', { className: 'grid grid-cols-2 gap-2', children: [
      jsx(Input, { value: contract, onChange: event => setContract(event.target.value), placeholder: 'Polygon contract: 0x + 40 hex', 'aria-label': 'Polygon contract' }),
      jsx('select', { value: rarity, onChange: event => setRarity(event.target.value), className: 'rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) px-3 py-2', 'aria-label': 'Rarity', children: [jsx('option', {value:'Exotic', children:'Exotic'}), jsx('option', {value:'Legendary', children:'Legendary'})] }),
      jsx(Input, { value: display, onChange: event => setDisplay(event.target.value), placeholder: 'Optional display name', 'aria-label': 'Display name' }),
      jsx(Input, { value: category, onChange: event => setCategory(event.target.value), placeholder: 'Optional category', 'aria-label': 'Category' })
    ]}),
    jsxs('section', { className: 'grid grid-cols-4 gap-2', children: [
      jsx(Button, { type:'button', onClick:() => void runAction(request('add')), children:'Add (Live)' }),
      jsx(Button, { type:'button', variant:'outline', onClick:() => void runAction(request('disable')), children:'Disable' }),
      jsx(Button, { type:'button', variant:'outline', onClick:() => void runAction(request('remove')), children:'Remove' }),
      jsx(Button, { type:'button', variant:'outline', onClick:() => void runAction({action:'list'}), children:'List Saved' })
    ]}),
    jsx(Result, {})
  ]})
}

export default {
  id: 'rarity-watch-manager',
  name: 'Rarity Watch Manager',
  defaultEnabled: true,
  register(ctx) {
    pluginCtx = ctx
    ctx.registerMany([
      { id:'pane', area:PANES_AREA, title:'Rarity Watch Manager', data:{placement:'bottom',dock:{pane:'workspace',pos:'bottom'},height:'390px'}, render:() => jsx(Panel,{}) },
      { id:'status', area:STATUSBAR_AREAS.right, order:184, render:() => jsx(Button,{type:'button',variant:'ghost',size:'sm',onClick:() => void runAction({action:'list'}),children:'Rarity Watches'}) },
      { id:'list', area:PALETTE_AREA, data:{id:'rarity-watch-manager.list',label:'Rarity Watches: List Saved (No Live Calls)',keywords:['rarity','exotic','legendary'],run:() => void runAction({action:'list'})} }
    ])
  }
}
