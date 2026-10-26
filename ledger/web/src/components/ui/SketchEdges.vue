<script setup>
import { tokens } from '../../design/tokens.js'
</script>
<template>
  <svg class="sketch-edge-defs" width="0" height="0" aria-hidden="true" focusable="false">
    <defs><filter id="ledger-pencil-edge" x="-4%" y="-4%" width="108%" height="108%" color-interpolation-filters="sRGB">
      <feTurbulence type="fractalNoise" :baseFrequency="tokens.sketch.frequency" numOctaves="2" :seed="tokens.sketch.seed" result="paper-noise" />
      <feDisplacementMap in="SourceGraphic" in2="paper-noise" :scale="tokens.sketch.roughness" xChannelSelector="R" yChannelSelector="G" />
    </filter></defs>
  </svg>
</template>
