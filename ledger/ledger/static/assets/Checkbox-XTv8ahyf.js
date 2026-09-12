import{ad as be,av as ue,Q as N,R as b,ah as K,aH as L,G as B,aI as E,aA as he,I as P,aP as fe,aL as U,aK as d,ae as C,af as n,aE as M,aF as $,bi as ve,bj as ke,bk as me,S as xe,aU as ge,aJ as pe,ai as G,aM as Ce,aN as ye,bn as ze,aD as Re,ao as we,aj as j}from"./index-BxRnIU1M.js";const Se={sizeSmall:"14px",sizeMedium:"16px",sizeLarge:"18px",labelPadding:"0 8px",labelFontWeight:"400"};function De(o){const{baseColor:c,inputColorDisabled:u,cardColor:y,modalColor:S,popoverColor:f,textColorDisabled:v,borderColor:i,primaryColor:k,textColor2:a,fontSizeSmall:m,fontSizeMedium:t,fontSizeLarge:r,borderRadiusSmall:x,lineHeight:h}=o;return Object.assign(Object.assign({},Se),{labelLineHeight:h,fontSizeSmall:m,fontSizeMedium:t,fontSizeLarge:r,borderRadius:x,color:c,colorChecked:k,colorDisabled:u,colorDisabledChecked:u,colorTableHeader:y,colorTableHeaderModal:S,colorTableHeaderPopover:f,checkMarkColor:c,checkMarkColorDisabled:v,checkMarkColorDisabledChecked:v,border:`1px solid ${i}`,borderDisabled:`1px solid ${i}`,borderDisabledChecked:`1px solid ${i}`,borderChecked:`1px solid ${k}`,borderFocus:`1px solid ${k}`,boxShadowFocus:`0 0 0 2px ${ue(k,{alpha:.3})}`,textColor:a,textColorDisabled:v})}const Te={name:"Checkbox",common:be,self:De},O=he("n-checkbox-group"),$e={min:Number,max:Number,size:String,value:Array,defaultValue:{type:Array,default:null},disabled:{type:Boolean,default:void 0},"onUpdate:value":[Function,Array],onUpdateValue:[Function,Array],onChange:[Function,Array]},Ae=N({name:"CheckboxGroup",props:$e,setup(o){const{mergedClsPrefixRef:c}=K(o),u=L(o),{mergedSizeRef:y,mergedDisabledRef:S}=u,f=B(o.defaultValue),v=P(()=>o.value),i=E(v,f),k=P(()=>{var t;return((t=i.value)===null||t===void 0?void 0:t.length)||0}),a=P(()=>Array.isArray(i.value)?new Set(i.value):new Set);function m(t,r){const{nTriggerFormInput:x,nTriggerFormChange:h}=u,{onChange:l,"onUpdate:value":z,onUpdateValue:R}=o;if(Array.isArray(i.value)){const s=Array.from(i.value),I=s.findIndex(A=>A===r);t?~I||(s.push(r),R&&d(R,s,{actionType:"check",value:r}),z&&d(z,s,{actionType:"check",value:r}),x(),h(),f.value=s,l&&d(l,s)):~I&&(s.splice(I,1),R&&d(R,s,{actionType:"uncheck",value:r}),z&&d(z,s,{actionType:"uncheck",value:r}),l&&d(l,s),f.value=s,x(),h())}else t?(R&&d(R,[r],{actionType:"check",value:r}),z&&d(z,[r],{actionType:"check",value:r}),l&&d(l,[r]),f.value=[r],x(),h()):(R&&d(R,[],{actionType:"uncheck",value:r}),z&&d(z,[],{actionType:"uncheck",value:r}),l&&d(l,[]),f.value=[],x(),h())}return fe(O,{checkedCountRef:k,maxRef:U(o,"max"),minRef:U(o,"min"),valueSetRef:a,disabledRef:S,mergedSizeRef:y,toggleCheckbox:m}),{mergedClsPrefix:c}},render(){return b("div",{class:`${this.mergedClsPrefix}-checkbox-group`,role:"group"},this.$slots)}}),Me=()=>b("svg",{viewBox:"0 0 64 64",class:"check-icon"},b("path",{d:"M50.42,16.76L22.34,39.45l-8.1-11.46c-1.12-1.58-3.3-1.96-4.88-0.84c-1.58,1.12-1.95,3.3-0.84,4.88l10.26,14.51  c0.56,0.79,1.42,1.31,2.38,1.45c0.16,0.02,0.32,0.03,0.48,0.03c0.8,0,1.57-0.27,2.2-0.78l30.99-25.03c1.5-1.21,1.74-3.42,0.52-4.92  C54.13,15.78,51.93,15.55,50.42,16.76z"})),_e=()=>b("svg",{viewBox:"0 0 100 100",class:"line-icon"},b("path",{d:"M80.2,55.5H21.4c-2.8,0-5.1-2.5-5.1-5.5l0,0c0-3,2.3-5.5,5.1-5.5h58.7c2.8,0,5.1,2.5,5.1,5.5l0,0C85.2,53.1,82.9,55.5,80.2,55.5z"})),Fe=C([n("checkbox",`
 font-size: var(--n-font-size);
 outline: none;
 cursor: pointer;
 display: inline-flex;
 flex-wrap: nowrap;
 align-items: flex-start;
 word-break: break-word;
 line-height: var(--n-size);
 --n-merged-color-table: var(--n-color-table);
 `,[M("show-label","line-height: var(--n-label-line-height);"),C("&:hover",[n("checkbox-box",[$("border","border: var(--n-border-checked);")])]),C("&:focus:not(:active)",[n("checkbox-box",[$("border",`
 border: var(--n-border-focus);
 box-shadow: var(--n-box-shadow-focus);
 `)])]),M("inside-table",[n("checkbox-box",`
 background-color: var(--n-merged-color-table);
 `)]),M("checked",[n("checkbox-box",`
 background-color: var(--n-color-checked);
 `,[n("checkbox-icon",[C(".check-icon",`
 opacity: 1;
 transform: scale(1);
 `)])])]),M("indeterminate",[n("checkbox-box",[n("checkbox-icon",[C(".check-icon",`
 opacity: 0;
 transform: scale(.5);
 `),C(".line-icon",`
 opacity: 1;
 transform: scale(1);
 `)])])]),M("checked, indeterminate",[C("&:focus:not(:active)",[n("checkbox-box",[$("border",`
 border: var(--n-border-checked);
 box-shadow: var(--n-box-shadow-focus);
 `)])]),n("checkbox-box",`
 background-color: var(--n-color-checked);
 border-left: 0;
 border-top: 0;
 `,[$("border",{border:"var(--n-border-checked)"})])]),M("disabled",{cursor:"not-allowed"},[M("checked",[n("checkbox-box",`
 background-color: var(--n-color-disabled-checked);
 `,[$("border",{border:"var(--n-border-disabled-checked)"}),n("checkbox-icon",[C(".check-icon, .line-icon",{fill:"var(--n-check-mark-color-disabled-checked)"})])])]),n("checkbox-box",`
 background-color: var(--n-color-disabled);
 `,[$("border",`
 border: var(--n-border-disabled);
 `),n("checkbox-icon",[C(".check-icon, .line-icon",`
 fill: var(--n-check-mark-color-disabled);
 `)])]),$("label",`
 color: var(--n-text-color-disabled);
 `)]),n("checkbox-box-wrapper",`
 position: relative;
 width: var(--n-size);
 flex-shrink: 0;
 flex-grow: 0;
 user-select: none;
 -webkit-user-select: none;
 `),n("checkbox-box",`
 position: absolute;
 left: 0;
 top: 50%;
 transform: translateY(-50%);
 height: var(--n-size);
 width: var(--n-size);
 display: inline-block;
 box-sizing: border-box;
 border-radius: var(--n-border-radius);
 background-color: var(--n-color);
 transition: background-color 0.3s var(--n-bezier);
 `,[$("border",`
 transition:
 border-color .3s var(--n-bezier),
 box-shadow .3s var(--n-bezier);
 border-radius: inherit;
 position: absolute;
 left: 0;
 right: 0;
 top: 0;
 bottom: 0;
 border: var(--n-border);
 `),n("checkbox-icon",`
 display: flex;
 align-items: center;
 justify-content: center;
 position: absolute;
 left: 1px;
 right: 1px;
 top: 1px;
 bottom: 1px;
 `,[C(".check-icon, .line-icon",`
 width: 100%;
 fill: var(--n-check-mark-color);
 opacity: 0;
 transform: scale(0.5);
 transform-origin: center;
 transition:
 fill 0.3s var(--n-bezier),
 transform 0.3s var(--n-bezier),
 opacity 0.3s var(--n-bezier),
 border-color 0.3s var(--n-bezier);
 `),ve({left:"1px",top:"1px"})])]),$("label",`
 color: var(--n-text-color);
 transition: color .3s var(--n-bezier);
 user-select: none;
 -webkit-user-select: none;
 padding: var(--n-label-padding);
 font-weight: var(--n-label-font-weight);
 `,[C("&:empty",{display:"none"})])]),ke(n("checkbox",`
 --n-merged-color-table: var(--n-color-table-modal);
 `)),me(n("checkbox",`
 --n-merged-color-table: var(--n-color-table-popover);
 `))]),Ie=Object.assign(Object.assign({},G.props),{size:String,checked:{type:[Boolean,String,Number],default:void 0},defaultChecked:{type:[Boolean,String,Number],default:!1},value:[String,Number],disabled:{type:Boolean,default:void 0},indeterminate:Boolean,label:String,focusable:{type:Boolean,default:!0},checkedValue:{type:[Boolean,String,Number],default:!0},uncheckedValue:{type:[Boolean,String,Number],default:!1},"onUpdate:checked":[Function,Array],onUpdateChecked:[Function,Array],privateInsideTable:Boolean,onChange:[Function,Array]}),Be=N({name:"Checkbox",props:Ie,setup(o){const c=Re(O,null),u=B(null),{mergedClsPrefixRef:y,inlineThemeDisabled:S,mergedRtlRef:f,mergedComponentPropsRef:v}=K(o),i=B(o.defaultChecked),k=U(o,"checked"),a=E(k,i),m=pe(()=>{if(c){const e=c.valueSetRef.value;return e&&o.value!==void 0?e.has(o.value):!1}else return a.value===o.checkedValue}),t=L(o,{mergedSize(e){var g,p;const{size:w}=o;if(w!==void 0)return w;if(c){const{value:T}=c.mergedSizeRef;if(T!==void 0)return T}if(e){const{mergedSize:T}=e;if(T!==void 0)return T.value}const D=(p=(g=v?.value)===null||g===void 0?void 0:g.Checkbox)===null||p===void 0?void 0:p.size;return D||"medium"},mergedDisabled(e){const{disabled:g}=o;if(g!==void 0)return g;if(c){if(c.disabledRef.value)return!0;const{maxRef:{value:p},checkedCountRef:w}=c;if(p!==void 0&&w.value>=p&&!m.value)return!0;const{minRef:{value:D}}=c;if(D!==void 0&&w.value<=D&&m.value)return!0}return e?e.disabled.value:!1}}),{mergedDisabledRef:r,mergedSizeRef:x}=t,h=G("Checkbox","-checkbox",Fe,Te,o,y);function l(e){if(c&&o.value!==void 0)c.toggleCheckbox(!m.value,o.value);else{const{onChange:g,"onUpdate:checked":p,onUpdateChecked:w}=o,{nTriggerFormInput:D,nTriggerFormChange:T}=t,F=m.value?o.uncheckedValue:o.checkedValue;p&&d(p,F,e),w&&d(w,F,e),g&&d(g,F,e),D(),T(),i.value=F}}function z(e){r.value||l(e)}function R(e){if(!r.value)switch(e.key){case" ":case"Enter":l(e)}}function s(e){e.key===" "&&e.preventDefault()}const I={focus:()=>{var e;(e=u.value)===null||e===void 0||e.focus()},blur:()=>{var e;(e=u.value)===null||e===void 0||e.blur()}},A=Ce("Checkbox",f,y),H=P(()=>{const{value:e}=x,{common:{cubicBezierEaseInOut:g},self:{borderRadius:p,color:w,colorChecked:D,colorDisabled:T,colorTableHeader:F,colorTableHeaderModal:V,colorTableHeaderPopover:W,checkMarkColor:J,checkMarkColorDisabled:Q,border:Y,borderFocus:q,borderDisabled:X,borderChecked:Z,boxShadowFocus:ee,textColor:oe,textColorDisabled:re,checkMarkColorDisabledChecked:ae,colorDisabledChecked:ne,borderDisabledChecked:ce,labelPadding:le,labelLineHeight:ie,labelFontWeight:de,[j("fontSize",e)]:te,[j("size",e)]:se}}=h.value;return{"--n-label-line-height":ie,"--n-label-font-weight":de,"--n-size":se,"--n-bezier":g,"--n-border-radius":p,"--n-border":Y,"--n-border-checked":Z,"--n-border-focus":q,"--n-border-disabled":X,"--n-border-disabled-checked":ce,"--n-box-shadow-focus":ee,"--n-color":w,"--n-color-checked":D,"--n-color-table":F,"--n-color-table-modal":V,"--n-color-table-popover":W,"--n-color-disabled":T,"--n-color-disabled-checked":ne,"--n-text-color":oe,"--n-text-color-disabled":re,"--n-check-mark-color":J,"--n-check-mark-color-disabled":Q,"--n-check-mark-color-disabled-checked":ae,"--n-font-size":te,"--n-label-padding":le}}),_=S?ye("checkbox",P(()=>x.value[0]),H,o):void 0;return Object.assign(t,I,{rtlEnabled:A,selfRef:u,mergedClsPrefix:y,mergedDisabled:r,renderedChecked:m,mergedTheme:h,labelId:ze(),handleClick:z,handleKeyUp:R,handleKeyDown:s,cssVars:S?void 0:H,themeClass:_?.themeClass,onRender:_?.onRender})},render(){var o;const{$slots:c,renderedChecked:u,mergedDisabled:y,indeterminate:S,privateInsideTable:f,cssVars:v,labelId:i,label:k,mergedClsPrefix:a,focusable:m,handleKeyUp:t,handleKeyDown:r,handleClick:x}=this;(o=this.onRender)===null||o===void 0||o.call(this);const h=xe(c.default,l=>k||l?b("span",{class:`${a}-checkbox__label`,id:i},k||l):null);return b("div",{ref:"selfRef",class:[`${a}-checkbox`,this.themeClass,this.rtlEnabled&&`${a}-checkbox--rtl`,u&&`${a}-checkbox--checked`,y&&`${a}-checkbox--disabled`,S&&`${a}-checkbox--indeterminate`,f&&`${a}-checkbox--inside-table`,h&&`${a}-checkbox--show-label`],tabindex:y||!m?void 0:0,role:"checkbox","aria-checked":S?"mixed":u,"aria-labelledby":i,style:v,onKeyup:t,onKeydown:r,onClick:x,onMousedown:()=>{we("selectstart",window,l=>{l.preventDefault()},{once:!0})}},b("div",{class:`${a}-checkbox-box-wrapper`}," ",b("div",{class:`${a}-checkbox-box`},b(ge,null,{default:()=>this.indeterminate?b("div",{key:"indeterminate",class:`${a}-checkbox-icon`},_e()):b("div",{key:"check",class:`${a}-checkbox-icon`},Me())}),b("div",{class:`${a}-checkbox-box__border`}))),h)}});export{Ae as N,Be as _,Te as c};
