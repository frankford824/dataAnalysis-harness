<script setup>
import {onMounted,onUnmounted,ref} from 'vue'
import {useMessage} from 'naive-ui'
import {api} from '../api'
import {useApp} from '../store'
import {setTelemetryAuthenticated} from '../telemetry'
const app=useApp(),message=useMessage(),actor=ref(null),show=ref(false),name=ref(''),password=ref(''),error=ref(''),busy=ref(false)
async function refresh(){try{actor.value=(await api.operatorStatus()).actor||null}catch{actor.value=null}setTelemetryAuthenticated(!!actor.value)}
function required(event){error.value=typeof event.detail==='string'?event.detail:'请先登录操作账号';show.value=true}
async function login(){
  if(!name.value.trim()||!password.value)return
  busy.value=true;error.value=''
  try{actor.value=(await api.operatorLogin(name.value.trim(),password.value)).actor;setTelemetryAuthenticated(!!actor.value);password.value='';show.value=false;app.uiRefresh++;message.success('已登录，请继续刚才的操作')}
  catch(reason){error.value=reason.message}finally{busy.value=false}
}
async function logout(){busy.value=true;try{await api.operatorLogout();actor.value=null;setTelemetryAuthenticated(false);password.value='';message.success('已退出登录')}catch(reason){message.error(reason.message)}finally{busy.value=false}}
onMounted(()=>{refresh();window.addEventListener('ledger:auth-required',required);window.addEventListener('focus',refresh)})
onUnmounted(()=>{window.removeEventListener('ledger:auth-required',required);window.removeEventListener('focus',refresh)})
</script>
<template>
  <div class="operator-session" aria-label="操作账号"><template v-if="actor"><span>{{actor.name||actor.id}}</span><n-button text size="small" :loading="busy" @click="logout">退出登录</n-button></template><template v-else><span>可查看结果，业务操作需登录</span><n-button size="small" @click="show=true">登录操作账号</n-button></template></div>
  <n-modal v-model:show="show" preset="card" title="登录操作账号" style="width:420px;max-width:calc(100vw - 32px)" @after-leave="password=''">
    <form @submit.prevent="login"><label class="operator-field">账号<n-input v-model:value="name" autocomplete="username" aria-label="操作账号名称"/></label><label class="operator-field">密码<n-input v-model:value="password" type="password" autocomplete="current-password" show-password-on="click" aria-label="操作账号密码"/></label><n-alert v-if="error" type="warning" style="margin:12px 0">{{error}}</n-alert><n-button attr-type="submit" type="primary" :loading="busy" :disabled="!name.trim()||!password" block>登录</n-button></form>
  </n-modal>
</template>
<style scoped>.operator-session{display:flex;justify-content:flex-end;align-items:center;gap:12px;min-height:44px;padding:6px 28px;color:#657184;font-size:12px;border-bottom:1px solid #e2e5eb;background:#fff}.operator-field{display:grid;gap:6px;font-size:13px;margin-bottom:16px}@media(max-width:760px){.operator-session{padding:8px 16px;gap:8px}.operator-session>span{font-size:11px}}</style>
