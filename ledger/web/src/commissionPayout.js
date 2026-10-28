/** Prefill the manual close payout box with the after-labor trial.

The order-level engine amount is before the store labor cut. Confirming that
raw number as `manual_amounts_after_labor` pays the labor cut as commission.
*/
export function suggestedManualPayout(person, {complete = true} = {}) {
  const trial = person?.amount
  const suggested = person?.amount_after_labor
  const value = suggested ?? trial
  return {
    trial: trial ?? null,
    suggested: suggested ?? null,
    amount: complete !== true || !['number','string'].includes(typeof value)
      || (typeof value === 'string' && !/^-?\d+(?:\.\d+)?$/.test(value.trim()))
      || !Number.isFinite(Number(value)) ? '' : Number(value).toFixed(2),
  }
}
