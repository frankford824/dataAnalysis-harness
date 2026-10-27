import tokens from './tokens.json' with { type: 'json' }

export { tokens }

// UI-only values. Accounting values, formatters and request payloads never pass through this module.
export function tokenCss() {
  const values = []
  for (const [name, color] of Object.entries(tokens.colors)) values.push(`--sk-color-${name}:light-dark(${color.light},${color.dark});`)
  for (const [group, prefix, unit] of [
    ['type', 'type', 'px'], ['space', 'space', 'px'], ['stroke', 'stroke', 'px'],
    ['geometry', 'geometry', 'px'], ['motion', 'motion', ''], ['weights', 'weight', ''],
    ['leading', 'leading', ''], ['fonts', 'font', ''], ['radius', 'radius', ''], ['shadows', 'shadow', ''],
  ]) for (const [name, value] of Object.entries(tokens[group])) values.push(`--sk-${prefix}-${name}:${value}${unit};`)
  values.push(`--sk-sketch-trace-opacity:${tokens.sketch.traceOpacity};`)
  return `/* Generated from design/tokens.json. Run node scripts/design-tokens.mjs. */\n:root{${values.join('')}}\n`
}

export function palette(dark = false) {
  return Object.fromEntries(Object.entries(tokens.colors).map(([name, value]) => [name, value[dark ? 'dark' : 'light']]))
}

export function uiTheme(dark = false) {
  const c = palette(dark), px = value => `${value}px`
  return {
    common: {
      primaryColor: c.action, primaryColorHover: c.action, primaryColorPressed: c.action, primaryColorSuppl: c.action,
      successColor: c.success, successColorHover: c.success, successColorPressed: c.success,
      warningColor: c.warning, warningColorHover: c.warning, warningColorPressed: c.warning,
      errorColor: c.danger, errorColorHover: c.danger, errorColorPressed: c.danger,
      infoColor: c.muted, textColorBase: c.ink, textColor1: c.ink, textColor2: c.ink, textColor3: c.muted,
      borderColor: c.line, dividerColor: c.line, hoverColor: c.surface, bodyColor: c.canvas,
      cardColor: c.paper, modalColor: c.paper, popoverColor: c.paper, tableHeaderColor: c.surface,
      borderRadius: px(tokens.space['1']), borderRadiusSmall: px(tokens.space['1']),
      fontSize: px(tokens.type.body), fontSizeSmall: px(tokens.type.caption), fontSizeMedium: px(tokens.type.body), fontSizeLarge: px(tokens.type.section),
      fontFamily: tokens.fonts.body, fontFamilyMono: tokens.fonts.numbers,
    },
    Card: { borderRadius: px(tokens.space['2']) },
    Button: { heightMedium: px(tokens.geometry['control-height']), heightSmall: px(tokens.geometry['control-height']), heightTiny: px(tokens.space['8']), fontSizeSmall: px(tokens.type.body), fontWeight: String(tokens.weights.medium) },
    Input: { heightMedium: px(tokens.geometry['control-height']), heightSmall: px(tokens.geometry['control-height']), fontSizeSmall: px(tokens.type.body), color: c.paper, colorFocus: c.paper, textColor: c.ink, placeholderColor: c.muted, border: `1px solid ${c.pencil}`, borderHover: `1px solid ${c.action}`, borderFocus: `1px solid ${c.action}`, boxShadowFocus: `0 0 0 3px ${c['action-soft']}` },
    Select: { peers: { InternalSelection: { heightMedium: px(tokens.geometry['control-height']), heightSmall: px(tokens.geometry['control-height']), fontSizeSmall: px(tokens.type.body), color: c.paper, colorActive: c.paper, textColor: c.ink, placeholderColor: c.muted, border: `1px solid ${c.pencil}`, borderHover: `1px solid ${c.action}`, borderActive: `1px solid ${c.action}`, borderFocus: `1px solid ${c.action}`, boxShadowActive: `0 0 0 3px ${c['action-soft']}`, boxShadowFocus: `0 0 0 3px ${c['action-soft']}`, arrowColor: c.muted } } },
    Tabs: { tabFontSizeMedium: px(tokens.type.body), tabFontSizeSmall: px(tokens.type.body), tabGapMedium: px(tokens.space['6']) },
    DataTable: { thColor: c.surface, thTextColor: c.muted, thFontWeight: String(tokens.weights.medium), tdColorHover: c.surface, borderColor: c.line, fontSizeSmall: px(tokens.type.body) },
    Layout: { siderColor: c.paper, headerColor: c.paper },
    Empty: { textColor: c.muted, extraTextColor: c.ink, iconColor: c.muted },
    Badge: { color: c.danger, colorError: c.danger, colorWarning: c.warning, colorSuccess: c.success, colorInfo: c.action, fontSize: px(tokens.type.caption) },
  }
}
