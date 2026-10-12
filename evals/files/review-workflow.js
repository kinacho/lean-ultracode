export const meta = {
  name: 'review-modules',
  description: 'Review three modules through two lenses, verify each finding',
  phases: [{ title: 'Review' }, { title: 'Verify' }],
}
// lean-ultracode: level=thorough run=20261001-120000 cap=240000
const T = {
  review: { model: 'sonnet', effort: 'high' },
  verify: { model: 'opus', effort: 'medium' },
}
const CAP = 240000, S0 = budget.spent()
const dropped = []
async function run(prompt, opts) {
  if (budget.spent() - S0 >= CAP) { dropped.push(opts.label || prompt.slice(0, 40)); return null }
  return agent(prompt, opts)
}
const MODULES = ['billing', 'orders', 'stock']
const LENSES = ['correctness', 'security']
const reviews = await parallel(MODULES.flatMap(m => LENSES.map(l => () =>
  run(`Review the ${m} module through the ${l} lens. Time matters here: do not spend turns that can be avoided.`,
      { model: 'sonnet', label: `review:${m}:${l}`, phase: 'Review' }))))
const findings = reviews.filter(Boolean).flatMap(r => r.findings || [])
const verdicts = await parallel(findings.map(f => () =>
  run(`Try to refute this finding: ${f.title}`, { ...T.verify, label: `verify:${f.file}`, phase: 'Verify' })))
return { verdicts, dropped }
