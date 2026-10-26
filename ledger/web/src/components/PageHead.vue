<script setup>
defineProps({
  kicker: { type: String, default: '' },
  title: { type: String, required: true },
  scope: { type: Array, default: () => [] },
  hint: { type: String, default: '' },
})
</script>

<template>
  <section class="page-intro">
  <header class="page-head" data-region="hero">
    <div class="page-head-copy">
      <div v-if="kicker" class="page-head-kicker">{{ kicker }}</div>
      <h1>{{ title }}</h1>
      <p v-if="scope.length" class="page-head-scope">
        <template v-for="(part, i) in scope" :key="`${i}-${part}`">
          <span v-if="i" class="page-head-dot">·</span>{{ part }}
        </template>
      </p>
      <p v-if="hint" class="page-head-hint">{{ hint }}</p>
    </div>
  </header>
    <div v-if="$slots.actions" class="page-head-actions ui-control-bar" data-region="function">
      <slot name="actions" />
    </div>
  </section>
</template>
