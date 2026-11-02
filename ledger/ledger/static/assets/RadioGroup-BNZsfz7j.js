import{U as ae,aE as O,W as _,X as p,b9 as u,Y as w,aN as E,aL as ie,$ as j,bs as M,g as V,bb as G,b1 as L,V as de,bc as D,bd as U,Z as N,T as B,ae as se,a0 as A,aV as W,aY as K,f as I,at as T,bt as le,a2 as ce}from"./index-2yrtDCR_.js";import{g as ue}from"./get-slot-Bk_rJcZu.js";const be={radioSizeSmall:"14px",radioSizeMedium:"16px",radioSizeLarge:"18px",labelPadding:"0 8px",labelFontWeight:"400"};function he(o){const{borderColor:e,primaryColor:t,baseColor:n,textColorDisabled:a,inputColorDisabled:f,textColor2:l,opacityDisabled:c,borderRadius:d,fontSizeSmall:b,fontSizeMedium:m,fontSizeLarge:x,heightSmall:h,heightMedium:C,heightLarge:v,lineHeight:R}=o;return Object.assign(Object.assign({},be),{labelLineHeight:R,buttonHeightSmall:h,buttonHeightMedium:C,buttonHeightLarge:v,fontSizeSmall:b,fontSizeMedium:m,fontSizeLarge:x,boxShadow:`inset 0 0 0 1px ${e}`,boxShadowActive:`inset 0 0 0 1px ${t}`,boxShadowFocus:`inset 0 0 0 1px ${t}, 0 0 0 2px ${O(t,{alpha:.2})}`,boxShadowHover:`inset 0 0 0 1px ${t}`,boxShadowDisabled:`inset 0 0 0 1px ${e}`,color:n,colorDisabled:f,colorActive:"#0000",textColor:l,textColorDisabled:a,dotColorActive:t,dotColorDisabled:e,buttonBorderColor:e,buttonBorderColorActive:t,buttonBorderColorHover:e,buttonColor:n,buttonColorActive:n,buttonTextColor:l,buttonTextColorActive:t,buttonTextColorHover:t,opacityDisabled:c,buttonBoxShadowFocus:`inset 0 0 0 1px ${t}, 0 0 0 2px ${O(t,{alpha:.3})}`,buttonBoxShadowHover:"inset 0 0 0 1px #0000",buttonBoxShadow:"inset 0 0 0 1px #0000",buttonBorderRadius:d})}const Y={name:"Radio",common:ae,self:he},ve=_("radio",`
 line-height: var(--n-label-line-height);
 outline: none;
 position: relative;
 user-select: none;
 -webkit-user-select: none;
 display: inline-flex;
 align-items: flex-start;
 flex-wrap: nowrap;
 font-size: var(--n-font-size);
 word-break: break-word;
`,[p("checked",[u("dot",`
 background-color: var(--n-color-active);
 `)]),u("dot-wrapper",`
 position: relative;
 flex-shrink: 0;
 flex-grow: 0;
 width: var(--n-radio-size);
 `),_("radio-input",`
 position: absolute;
 border: 0;
 width: 0;
 height: 0;
 opacity: 0;
 margin: 0;
 `),u("dot",`
 position: absolute;
 top: 50%;
 left: 0;
 transform: translateY(-50%);
 height: var(--n-radio-size);
 width: var(--n-radio-size);
 background: var(--n-color);
 box-shadow: var(--n-box-shadow);
 border-radius: 50%;
 transition:
 background-color .3s var(--n-bezier),
 box-shadow .3s var(--n-bezier);
 `,[w("&::before",`
 content: "";
 opacity: 0;
 position: absolute;
 left: 4px;
 top: 4px;
 height: calc(100% - 8px);
 width: calc(100% - 8px);
 border-radius: 50%;
 transform: scale(.8);
 background: var(--n-dot-color-active);
 transition: 
 opacity .3s var(--n-bezier),
 background-color .3s var(--n-bezier),
 transform .3s var(--n-bezier);
 `),p("checked",{boxShadow:"var(--n-box-shadow-active)"},[w("&::before",`
 opacity: 1;
 transform: scale(1);
 `)])]),u("label",`
 color: var(--n-text-color);
 padding: var(--n-label-padding);
 font-weight: var(--n-label-font-weight);
 display: inline-block;
 transition: color .3s var(--n-bezier);
 `),E("disabled",`
 cursor: pointer;
 `,[w("&:hover",[u("dot",{boxShadow:"var(--n-box-shadow-hover)"})]),p("focus",[w("&:not(:active)",[u("dot",{boxShadow:"var(--n-box-shadow-focus)"})])])]),p("disabled",`
 cursor: not-allowed;
 `,[u("dot",{boxShadow:"var(--n-box-shadow-disabled)",backgroundColor:"var(--n-color-disabled)"},[w("&::before",{backgroundColor:"var(--n-dot-color-disabled)"}),p("checked",`
 opacity: 1;
 `)]),u("label",{color:"var(--n-text-color-disabled)"}),_("radio-input",`
 cursor: not-allowed;
 `)])]),fe={name:String,value:{type:[String,Number,Boolean],default:"on"},checked:{type:Boolean,default:void 0},defaultChecked:Boolean,disabled:{type:Boolean,default:void 0},label:String,size:String,onUpdateChecked:[Function,Array],"onUpdate:checked":[Function,Array],checkedValue:{type:Boolean,default:void 0}},X=de("n-radio-group");function ge(o){const e=ie(X,null),{mergedClsPrefixRef:t,mergedComponentPropsRef:n}=j(o),a=M(o,{mergedSize(r){var i,s;const{size:g}=o;if(g!==void 0)return g;if(e){const{mergedSizeRef:{value:F}}=e;if(F!==void 0)return F}if(r)return r.mergedSize.value;const $=(s=(i=n?.value)===null||i===void 0?void 0:i.Radio)===null||s===void 0?void 0:s.size;return $||"medium"},mergedDisabled(r){return!!(o.disabled||e?.disabledRef.value||r?.disabled.value)}}),{mergedSizeRef:f,mergedDisabledRef:l}=a,c=V(null),d=V(null),b=V(o.defaultChecked),m=U(o,"checked"),x=G(m,b),h=L(()=>e?e.valueRef.value===o.value:x.value),C=L(()=>{const{name:r}=o;if(r!==void 0)return r;if(e)return e.nameRef.value}),v=V(!1);function R(){if(e){const{doUpdateValue:r}=e,{value:i}=o;D(r,i)}else{const{onUpdateChecked:r,"onUpdate:checked":i}=o,{nTriggerFormInput:s,nTriggerFormChange:g}=a;r&&D(r,!0),i&&D(i,!0),s(),g(),b.value=!0}}function z(){l.value||h.value||R()}function k(){z(),c.value&&(c.value.checked=h.value)}function y(){v.value=!1}function S(){v.value=!0}return{mergedClsPrefix:e?e.mergedClsPrefixRef:t,inputRef:c,labelRef:d,mergedName:C,mergedDisabled:l,renderSafeChecked:h,focus:v,mergedSize:f,handleRadioInputChange:k,handleRadioInputBlur:y,handleRadioInputFocus:S}}const pe=Object.assign(Object.assign({},A.props),fe),Se=N({name:"Radio",props:pe,setup(o){const e=ge(o),t=A("Radio","-radio",ve,Y,o,e.mergedClsPrefix),n=I(()=>{const{mergedSize:{value:b}}=e,{common:{cubicBezierEaseInOut:m},self:{boxShadow:x,boxShadowActive:h,boxShadowDisabled:C,boxShadowFocus:v,boxShadowHover:R,color:z,colorDisabled:k,colorActive:y,textColor:S,textColorDisabled:r,dotColorActive:i,dotColorDisabled:s,labelPadding:g,labelLineHeight:$,labelFontWeight:F,[T("fontSize",b)]:H,[T("radioSize",b)]:P}}=t.value;return{"--n-bezier":m,"--n-label-line-height":$,"--n-label-font-weight":F,"--n-box-shadow":x,"--n-box-shadow-active":h,"--n-box-shadow-disabled":C,"--n-box-shadow-focus":v,"--n-box-shadow-hover":R,"--n-color":z,"--n-color-active":y,"--n-color-disabled":k,"--n-dot-color-active":i,"--n-dot-color-disabled":s,"--n-font-size":H,"--n-radio-size":P,"--n-text-color":S,"--n-text-color-disabled":r,"--n-label-padding":g}}),{inlineThemeDisabled:a,mergedClsPrefixRef:f,mergedRtlRef:l}=j(o),c=W("Radio",l,f),d=a?K("radio",I(()=>e.mergedSize.value[0]),n,o):void 0;return Object.assign(e,{rtlEnabled:c,cssVars:a?void 0:n,themeClass:d?.themeClass,onRender:d?.onRender})},render(){const{$slots:o,mergedClsPrefix:e,onRender:t,label:n}=this;return t?.(),B("label",{class:[`${e}-radio`,this.themeClass,this.rtlEnabled&&`${e}-radio--rtl`,this.mergedDisabled&&`${e}-radio--disabled`,this.renderSafeChecked&&`${e}-radio--checked`,this.focus&&`${e}-radio--focus`],style:this.cssVars},B("div",{class:`${e}-radio__dot-wrapper`}," ",B("div",{class:[`${e}-radio__dot`,this.renderSafeChecked&&`${e}-radio__dot--checked`]}),B("input",{ref:"inputRef",type:"radio",class:`${e}-radio-input`,value:this.value,name:this.mergedName,checked:this.renderSafeChecked,disabled:this.mergedDisabled,onChange:this.handleRadioInputChange,onFocus:this.handleRadioInputFocus,onBlur:this.handleRadioInputBlur})),se(o.default,a=>!a&&!n?null:B("div",{ref:"labelRef",class:`${e}-radio__label`},a||n)))}}),me=_("radio-group",`
 display: inline-block;
 font-size: var(--n-font-size);
`,[u("splitor",`
 display: inline-block;
 vertical-align: bottom;
 width: 1px;
 transition:
 background-color .3s var(--n-bezier),
 opacity .3s var(--n-bezier);
 background: var(--n-button-border-color);
 `,[p("checked",{backgroundColor:"var(--n-button-border-color-active)"}),p("disabled",{opacity:"var(--n-opacity-disabled)"})]),p("button-group",`
 white-space: nowrap;
 height: var(--n-height);
 line-height: var(--n-height);
 `,[_("radio-button",{height:"var(--n-height)",lineHeight:"var(--n-height)"}),u("splitor",{height:"var(--n-height)"})]),_("radio-button",`
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
 `,[_("radio-input",`
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
 `),u("state-border",`
 z-index: 1;
 pointer-events: none;
 position: absolute;
 box-shadow: var(--n-button-box-shadow);
 transition: box-shadow .3s var(--n-bezier);
 left: -1px;
 bottom: -1px;
 right: -1px;
 top: -1px;
 `),w("&:first-child",`
 border-top-left-radius: var(--n-button-border-radius);
 border-bottom-left-radius: var(--n-button-border-radius);
 border-left: 1px solid var(--n-button-border-color);
 `,[u("state-border",`
 border-top-left-radius: var(--n-button-border-radius);
 border-bottom-left-radius: var(--n-button-border-radius);
 `)]),w("&:last-child",`
 border-top-right-radius: var(--n-button-border-radius);
 border-bottom-right-radius: var(--n-button-border-radius);
 border-right: 1px solid var(--n-button-border-color);
 `,[u("state-border",`
 border-top-right-radius: var(--n-button-border-radius);
 border-bottom-right-radius: var(--n-button-border-radius);
 `)]),E("disabled",`
 cursor: pointer;
 `,[w("&:hover",[u("state-border",`
 transition: box-shadow .3s var(--n-bezier);
 box-shadow: var(--n-button-box-shadow-hover);
 `),E("checked",{color:"var(--n-button-text-color-hover)"})]),p("focus",[w("&:not(:active)",[u("state-border",{boxShadow:"var(--n-button-box-shadow-focus)"})])])]),p("checked",`
 background: var(--n-button-color-active);
 color: var(--n-button-text-color-active);
 border-color: var(--n-button-border-color-active);
 `),p("disabled",`
 cursor: not-allowed;
 opacity: var(--n-opacity-disabled);
 `)])]);function xe(o,e,t){var n;const a=[];let f=!1;for(let l=0;l<o.length;++l){const c=o[l],d=(n=c.type)===null||n===void 0?void 0:n.name;d==="RadioButton"&&(f=!0);const b=c.props;if(d!=="RadioButton"){a.push(c);continue}if(l===0)a.push(c);else{const m=a[a.length-1].props,x=e===m.value,h=m.disabled,C=e===b.value,v=b.disabled,R=(x?2:0)+(h?0:1),z=(C?2:0)+(v?0:1),k={[`${t}-radio-group__splitor--disabled`]:h,[`${t}-radio-group__splitor--checked`]:x},y={[`${t}-radio-group__splitor--disabled`]:v,[`${t}-radio-group__splitor--checked`]:C},S=R<z?y:k;a.push(B("div",{class:[`${t}-radio-group__splitor`,S]}),c)}}return{children:a,isButtonGroup:f}}const Ce=Object.assign(Object.assign({},A.props),{name:String,value:[String,Number,Boolean],defaultValue:{type:[String,Number,Boolean],default:null},size:String,disabled:{type:Boolean,default:void 0},"onUpdate:value":[Function,Array],onUpdateValue:[Function,Array]}),ze=N({name:"RadioGroup",props:Ce,setup(o){const e=V(null),{mergedSizeRef:t,mergedDisabledRef:n,nTriggerFormChange:a,nTriggerFormInput:f,nTriggerFormBlur:l,nTriggerFormFocus:c}=M(o),{mergedClsPrefixRef:d,inlineThemeDisabled:b,mergedRtlRef:m}=j(o),x=A("Radio","-radio-group",me,Y,o,d),h=V(o.defaultValue),C=U(o,"value"),v=G(C,h);function R(i){const{onUpdateValue:s,"onUpdate:value":g}=o;s&&D(s,i),g&&D(g,i),h.value=i,a(),f()}function z(i){const{value:s}=e;s&&(s.contains(i.relatedTarget)||c())}function k(i){const{value:s}=e;s&&(s.contains(i.relatedTarget)||l())}ce(X,{mergedClsPrefixRef:d,nameRef:U(o,"name"),valueRef:v,disabledRef:n,mergedSizeRef:t,doUpdateValue:R});const y=W("Radio",m,d),S=I(()=>{const{value:i}=t,{common:{cubicBezierEaseInOut:s},self:{buttonBorderColor:g,buttonBorderColorActive:$,buttonBorderRadius:F,buttonBoxShadow:H,buttonBoxShadowFocus:P,buttonBoxShadowHover:Z,buttonColor:q,buttonColorActive:J,buttonTextColor:Q,buttonTextColorActive:ee,buttonTextColorHover:oe,opacityDisabled:te,[T("buttonHeight",i)]:re,[T("fontSize",i)]:ne}}=x.value;return{"--n-font-size":ne,"--n-bezier":s,"--n-button-border-color":g,"--n-button-border-color-active":$,"--n-button-border-radius":F,"--n-button-box-shadow":H,"--n-button-box-shadow-focus":P,"--n-button-box-shadow-hover":Z,"--n-button-color":q,"--n-button-color-active":J,"--n-button-text-color":Q,"--n-button-text-color-hover":oe,"--n-button-text-color-active":ee,"--n-height":re,"--n-opacity-disabled":te}}),r=b?K("radio-group",I(()=>t.value[0]),S,o):void 0;return{selfElRef:e,rtlEnabled:y,mergedClsPrefix:d,mergedValue:v,handleFocusout:k,handleFocusin:z,cssVars:b?void 0:S,themeClass:r?.themeClass,onRender:r?.onRender}},render(){var o;const{mergedValue:e,mergedClsPrefix:t,handleFocusin:n,handleFocusout:a}=this,{children:f,isButtonGroup:l}=xe(le(ue(this)),e,t);return(o=this.onRender)===null||o===void 0||o.call(this),B("div",{onFocusin:n,onFocusout:a,ref:"selfElRef",class:[`${t}-radio-group`,this.rtlEnabled&&`${t}-radio-group--rtl`,this.themeClass,l&&`${t}-radio-group--button-group`],style:this.cssVars},f)}});export{ze as _,Se as a,Y as b,fe as r,ge as s};
