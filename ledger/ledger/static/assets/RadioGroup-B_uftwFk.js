import{R as oe,ap as $,ax as te,Y as P,bj as E,G as z,b0 as j,aS as A,au as re,b1 as w,b2 as T,T as V,az as C,ay as S,S as I,aA as D,U as ne,bk as ae,V as M,Z as G,aB as ie,aC as de,I as H,$ as U,aV as se}from"./index-BQVQ0l1i.js";import{g as le}from"./get-slot-Bk_rJcZu.js";const ue={radioSizeSmall:"14px",radioSizeMedium:"16px",radioSizeLarge:"18px",labelPadding:"0 8px",labelFontWeight:"400"};function ce(e){const{borderColor:o,primaryColor:t,baseColor:i,textColorDisabled:d,inputColorDisabled:h,textColor2:s,opacityDisabled:l,borderRadius:u,fontSizeSmall:v,fontSizeMedium:f,fontSizeLarge:p,heightSmall:c,heightMedium:m,heightLarge:b,lineHeight:x}=e;return Object.assign(Object.assign({},ue),{labelLineHeight:x,buttonHeightSmall:c,buttonHeightMedium:m,buttonHeightLarge:b,fontSizeSmall:v,fontSizeMedium:f,fontSizeLarge:p,boxShadow:`inset 0 0 0 1px ${o}`,boxShadowActive:`inset 0 0 0 1px ${t}`,boxShadowFocus:`inset 0 0 0 1px ${t}, 0 0 0 2px ${$(t,{alpha:.2})}`,boxShadowHover:`inset 0 0 0 1px ${t}`,boxShadowDisabled:`inset 0 0 0 1px ${o}`,color:i,colorDisabled:h,colorActive:"#0000",textColor:s,textColorDisabled:d,dotColorActive:t,dotColorDisabled:o,buttonBorderColor:o,buttonBorderColorActive:t,buttonBorderColorHover:o,buttonColor:i,buttonColorActive:i,buttonTextColor:s,buttonTextColorActive:t,buttonTextColorHover:t,opacityDisabled:l,buttonBoxShadowFocus:`inset 0 0 0 1px ${t}, 0 0 0 2px ${$(t,{alpha:.3})}`,buttonBoxShadowHover:"inset 0 0 0 1px #0000",buttonBoxShadow:"inset 0 0 0 1px #0000",buttonBorderRadius:u})}const be={name:"Radio",common:oe,self:ce},me={name:String,value:{type:[String,Number,Boolean],default:"on"},checked:{type:Boolean,default:void 0},defaultChecked:Boolean,disabled:{type:Boolean,default:void 0},label:String,size:String,onUpdateChecked:[Function,Array],"onUpdate:checked":[Function,Array],checkedValue:{type:Boolean,default:void 0}},L=re("n-radio-group");function xe(e){const o=te(L,null),{mergedClsPrefixRef:t,mergedComponentPropsRef:i}=P(e),d=E(e,{mergedSize(r){var n,a;const{size:g}=e;if(g!==void 0)return g;if(o){const{mergedSizeRef:{value:F}}=o;if(F!==void 0)return F}if(r)return r.mergedSize.value;const _=(a=(n=i?.value)===null||n===void 0?void 0:n.Radio)===null||a===void 0?void 0:a.size;return _||"medium"},mergedDisabled(r){return!!(e.disabled||o?.disabledRef.value||r?.disabled.value)}}),{mergedSizeRef:h,mergedDisabledRef:s}=d,l=z(null),u=z(null),v=z(e.defaultChecked),f=T(e,"checked"),p=j(f,v),c=A(()=>o?o.valueRef.value===e.value:p.value),m=A(()=>{const{name:r}=e;if(r!==void 0)return r;if(o)return o.nameRef.value}),b=z(!1);function x(){if(o){const{doUpdateValue:r}=o,{value:n}=e;w(r,n)}else{const{onUpdateChecked:r,"onUpdate:checked":n}=e,{nTriggerFormInput:a,nTriggerFormChange:g}=d;r&&w(r,!0),n&&w(n,!0),a(),g(),v.value=!0}}function k(){s.value||c.value||x()}function y(){k(),l.value&&(l.value.checked=c.value)}function B(){b.value=!1}function R(){b.value=!0}return{mergedClsPrefix:o?o.mergedClsPrefixRef:t,inputRef:l,labelRef:u,mergedName:m,mergedDisabled:s,renderSafeChecked:c,focus:b,mergedSize:h,handleRadioInputChange:y,handleRadioInputBlur:B,handleRadioInputFocus:R}}const he=V("radio-group",`
 display: inline-block;
 font-size: var(--n-font-size);
`,[C("splitor",`
 display: inline-block;
 vertical-align: bottom;
 width: 1px;
 transition:
 background-color .3s var(--n-bezier),
 opacity .3s var(--n-bezier);
 background: var(--n-button-border-color);
 `,[S("checked",{backgroundColor:"var(--n-button-border-color-active)"}),S("disabled",{opacity:"var(--n-opacity-disabled)"})]),S("button-group",`
 white-space: nowrap;
 height: var(--n-height);
 line-height: var(--n-height);
 `,[V("radio-button",{height:"var(--n-height)",lineHeight:"var(--n-height)"}),C("splitor",{height:"var(--n-height)"})]),V("radio-button",`
 vertical-align: bottom;
 outline: none;
 position: relative;
 user-select: none;
 -webkit-user-select: none;
 display: inline-block;
 box-sizing: border-box;
 padding-left: 14px;
 padding-right: 14px;
 white-space: nowrap;
 transition:
 background-color .3s var(--n-bezier),
 opacity .3s var(--n-bezier),
 border-color .3s var(--n-bezier),
 color .3s var(--n-bezier);
 background: var(--n-button-color);
 color: var(--n-button-text-color);
 border-top: 1px solid var(--n-button-border-color);
 border-bottom: 1px solid var(--n-button-border-color);
 `,[V("radio-input",`
 pointer-events: none;
 position: absolute;
 border: 0;
 border-radius: inherit;
 left: 0;
 right: 0;
 top: 0;
 bottom: 0;
 opacity: 0;
 z-index: 1;
 `),C("state-border",`
 z-index: 1;
 pointer-events: none;
 position: absolute;
 box-shadow: var(--n-button-box-shadow);
 transition: box-shadow .3s var(--n-bezier);
 left: -1px;
 bottom: -1px;
 right: -1px;
 top: -1px;
 `),I("&:first-child",`
 border-top-left-radius: var(--n-button-border-radius);
 border-bottom-left-radius: var(--n-button-border-radius);
 border-left: 1px solid var(--n-button-border-color);
 `,[C("state-border",`
 border-top-left-radius: var(--n-button-border-radius);
 border-bottom-left-radius: var(--n-button-border-radius);
 `)]),I("&:last-child",`
 border-top-right-radius: var(--n-button-border-radius);
 border-bottom-right-radius: var(--n-button-border-radius);
 border-right: 1px solid var(--n-button-border-color);
 `,[C("state-border",`
 border-top-right-radius: var(--n-button-border-radius);
 border-bottom-right-radius: var(--n-button-border-radius);
 `)]),D("disabled",`
 cursor: pointer;
 `,[I("&:hover",[C("state-border",`
 transition: box-shadow .3s var(--n-bezier);
 box-shadow: var(--n-button-box-shadow-hover);
 `),D("checked",{color:"var(--n-button-text-color-hover)"})]),S("focus",[I("&:not(:active)",[C("state-border",{boxShadow:"var(--n-button-box-shadow-focus)"})])])]),S("checked",`
 background: var(--n-button-color-active);
 color: var(--n-button-text-color-active);
 border-color: var(--n-button-border-color-active);
 `),S("disabled",`
 cursor: not-allowed;
 opacity: var(--n-opacity-disabled);
 `)])]);function ve(e,o,t){var i;const d=[];let h=!1;for(let s=0;s<e.length;++s){const l=e[s],u=(i=l.type)===null||i===void 0?void 0:i.name;u==="RadioButton"&&(h=!0);const v=l.props;if(u!=="RadioButton"){d.push(l);continue}if(s===0)d.push(l);else{const f=d[d.length-1].props,p=o===f.value,c=f.disabled,m=o===v.value,b=v.disabled,x=(p?2:0)+(c?0:1),k=(m?2:0)+(b?0:1),y={[`${t}-radio-group__splitor--disabled`]:c,[`${t}-radio-group__splitor--checked`]:p},B={[`${t}-radio-group__splitor--disabled`]:b,[`${t}-radio-group__splitor--checked`]:m},R=x<k?B:y;d.push(M("div",{class:[`${t}-radio-group__splitor`,R]}),l)}}return{children:d,isButtonGroup:h}}const ge=Object.assign(Object.assign({},G.props),{name:String,value:[String,Number,Boolean],defaultValue:{type:[String,Number,Boolean],default:null},size:String,disabled:{type:Boolean,default:void 0},"onUpdate:value":[Function,Array],onUpdateValue:[Function,Array]}),Ce=ne({name:"RadioGroup",props:ge,setup(e){const o=z(null),{mergedSizeRef:t,mergedDisabledRef:i,nTriggerFormChange:d,nTriggerFormInput:h,nTriggerFormBlur:s,nTriggerFormFocus:l}=E(e),{mergedClsPrefixRef:u,inlineThemeDisabled:v,mergedRtlRef:f}=P(e),p=G("Radio","-radio-group",he,be,e,u),c=z(e.defaultValue),m=T(e,"value"),b=j(m,c);function x(n){const{onUpdateValue:a,"onUpdate:value":g}=e;a&&w(a,n),g&&w(g,n),c.value=n,d(),h()}function k(n){const{value:a}=o;a&&(a.contains(n.relatedTarget)||l())}function y(n){const{value:a}=o;a&&(a.contains(n.relatedTarget)||s())}se(L,{mergedClsPrefixRef:u,nameRef:T(e,"name"),valueRef:b,disabledRef:i,mergedSizeRef:t,doUpdateValue:x});const B=ie("Radio",f,u),R=H(()=>{const{value:n}=t,{common:{cubicBezierEaseInOut:a},self:{buttonBorderColor:g,buttonBorderColorActive:_,buttonBorderRadius:F,buttonBoxShadow:O,buttonBoxShadowFocus:N,buttonBoxShadowHover:K,buttonColor:W,buttonColorActive:Y,buttonTextColor:Z,buttonTextColorActive:q,buttonTextColorHover:J,opacityDisabled:Q,[U("buttonHeight",n)]:X,[U("fontSize",n)]:ee}}=p.value;return{"--n-font-size":ee,"--n-bezier":a,"--n-button-border-color":g,"--n-button-border-color-active":_,"--n-button-border-radius":F,"--n-button-box-shadow":O,"--n-button-box-shadow-focus":N,"--n-button-box-shadow-hover":K,"--n-button-color":W,"--n-button-color-active":Y,"--n-button-text-color":Z,"--n-button-text-color-hover":J,"--n-button-text-color-active":q,"--n-height":X,"--n-opacity-disabled":Q}}),r=v?de("radio-group",H(()=>t.value[0]),R,e):void 0;return{selfElRef:o,rtlEnabled:B,mergedClsPrefix:u,mergedValue:b,handleFocusout:y,handleFocusin:k,cssVars:v?void 0:R,themeClass:r?.themeClass,onRender:r?.onRender}},render(){var e;const{mergedValue:o,mergedClsPrefix:t,handleFocusin:i,handleFocusout:d}=this,{children:h,isButtonGroup:s}=ve(ae(le(this)),o,t);return(e=this.onRender)===null||e===void 0||e.call(this),M("div",{onFocusin:i,onFocusout:d,ref:"selfElRef",class:[`${t}-radio-group`,this.rtlEnabled&&`${t}-radio-group--rtl`,this.themeClass,s&&`${t}-radio-group--button-group`],style:this.cssVars},h)}});export{Ce as _,be as a,me as r,xe as s};
