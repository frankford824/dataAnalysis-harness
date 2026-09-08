import{R as be,ap as ue,U as N,V as b,Y as K,bj as V,G as A,b0 as L,au as he,I as B,aV as fe,b2 as U,b1 as d,S as C,T as n,ay as M,az as $,bb as ve,bc as ke,bd as xe,a1 as me,ai as ge,aI as pe,ax as Ce,aS as ye,Z as E,aB as ze,aC as Re,bg as we,$ as j}from"./index-BQVQ0l1i.js";const Se={sizeSmall:"14px",sizeMedium:"16px",sizeLarge:"18px",labelPadding:"0 8px",labelFontWeight:"400"};function Te(o){const{baseColor:c,inputColorDisabled:u,cardColor:y,modalColor:S,popoverColor:f,textColorDisabled:v,borderColor:i,primaryColor:k,textColor2:a,fontSizeSmall:x,fontSizeMedium:t,fontSizeLarge:r,borderRadiusSmall:m,lineHeight:h}=o;return Object.assign(Object.assign({},Se),{labelLineHeight:h,fontSizeSmall:x,fontSizeMedium:t,fontSizeLarge:r,borderRadius:m,color:c,colorChecked:k,colorDisabled:u,colorDisabledChecked:u,colorTableHeader:y,colorTableHeaderModal:S,colorTableHeaderPopover:f,checkMarkColor:c,checkMarkColorDisabled:v,checkMarkColorDisabledChecked:v,border:`1px solid ${i}`,borderDisabled:`1px solid ${i}`,borderDisabledChecked:`1px solid ${i}`,borderChecked:`1px solid ${k}`,borderFocus:`1px solid ${k}`,boxShadowFocus:`0 0 0 2px ${ue(k,{alpha:.3})}`,textColor:a,textColorDisabled:v})}const De={name:"Checkbox",common:be,self:Te},G=he("n-checkbox-group"),$e={min:Number,max:Number,size:String,value:Array,defaultValue:{type:Array,default:null},disabled:{type:Boolean,default:void 0},"onUpdate:value":[Function,Array],onUpdateValue:[Function,Array],onChange:[Function,Array]},Pe=N({name:"CheckboxGroup",props:$e,setup(o){const{mergedClsPrefixRef:c}=K(o),u=V(o),{mergedSizeRef:y,mergedDisabledRef:S}=u,f=A(o.defaultValue),v=B(()=>o.value),i=L(v,f),k=B(()=>{var t;return((t=i.value)===null||t===void 0?void 0:t.length)||0}),a=B(()=>Array.isArray(i.value)?new Set(i.value):new Set);function x(t,r){const{nTriggerFormInput:m,nTriggerFormChange:h}=u,{onChange:l,"onUpdate:value":z,onUpdateValue:R}=o;if(Array.isArray(i.value)){const s=Array.from(i.value),I=s.findIndex(P=>P===r);t?~I||(s.push(r),R&&d(R,s,{actionType:"check",value:r}),z&&d(z,s,{actionType:"check",value:r}),m(),h(),f.value=s,l&&d(l,s)):~I&&(s.splice(I,1),R&&d(R,s,{actionType:"uncheck",value:r}),z&&d(z,s,{actionType:"uncheck",value:r}),l&&d(l,s),f.value=s,m(),h())}else t?(R&&d(R,[r],{actionType:"check",value:r}),z&&d(z,[r],{actionType:"check",value:r}),l&&d(l,[r]),f.value=[r],m(),h()):(R&&d(R,[],{actionType:"uncheck",value:r}),z&&d(z,[],{actionType:"uncheck",value:r}),l&&d(l,[]),f.value=[],m(),h())}return fe(G,{checkedCountRef:k,maxRef:U(o,"max"),minRef:U(o,"min"),valueSetRef:a,disabledRef:S,mergedSizeRef:y,toggleCheckbox:x}),{mergedClsPrefix:c}},render(){return b("div",{class:`${this.mergedClsPrefix}-checkbox-group`,role:"group"},this.$slots)}}),Me=()=>b("svg",{viewBox:"0 0 64 64",class:"check-icon"},b("path",{d:"M50.42,16.76L22.34,39.45l-8.1-11.46c-1.12-1.58-3.3-1.96-4.88-0.84c-1.58,1.12-1.95,3.3-0.84,4.88l10.26,14.51  c0.56,0.79,1.42,1.31,2.38,1.45c0.16,0.02,0.32,0.03,0.48,0.03c0.8,0,1.57-0.27,2.2-0.78l30.99-25.03c1.5-1.21,1.74-3.42,0.52-4.92  C54.13,15.78,51.93,15.55,50.42,16.76z"})),_e=()=>b("svg",{viewBox:"0 0 100 100",class:"line-icon"},b("path",{d:"M80.2,55.5H21.4c-2.8,0-5.1-2.5-5.1-5.5l0,0c0-3,2.3-5.5,5.1-5.5h58.7c2.8,0,5.1,2.5,5.1,5.5l0,0C85.2,53.1,82.9,55.5,80.2,55.5z"})),Fe=C([n("checkbox",`
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
 `)),xe(n("checkbox",`
 --n-merged-color-table: var(--n-color-table-popover);
 `))]),Ie=Object.assign(Object.assign({},E.props),{size:String,checked:{type:[Boolean,String,Number],default:void 0},defaultChecked:{type:[Boolean,String,Number],default:!1},value:[String,Number],disabled:{type:Boolean,default:void 0},indeterminate:Boolean,label:String,focusable:{type:Boolean,default:!0},checkedValue:{type:[Boolean,String,Number],default:!0},uncheckedValue:{type:[Boolean,String,Number],default:!1},"onUpdate:checked":[Function,Array],onUpdateChecked:[Function,Array],privateInsideTable:Boolean,onChange:[Function,Array]}),Ae=N({name:"Checkbox",props:Ie,setup(o){const c=Ce(G,null),u=A(null),{mergedClsPrefixRef:y,inlineThemeDisabled:S,mergedRtlRef:f,mergedComponentPropsRef:v}=K(o),i=A(o.defaultChecked),k=U(o,"checked"),a=L(k,i),x=ye(()=>{if(c){const e=c.valueSetRef.value;return e&&o.value!==void 0?e.has(o.value):!1}else return a.value===o.checkedValue}),t=V(o,{mergedSize(e){var g,p;const{size:w}=o;if(w!==void 0)return w;if(c){const{value:D}=c.mergedSizeRef;if(D!==void 0)return D}if(e){const{mergedSize:D}=e;if(D!==void 0)return D.value}const T=(p=(g=v?.value)===null||g===void 0?void 0:g.Checkbox)===null||p===void 0?void 0:p.size;return T||"medium"},mergedDisabled(e){const{disabled:g}=o;if(g!==void 0)return g;if(c){if(c.disabledRef.value)return!0;const{maxRef:{value:p},checkedCountRef:w}=c;if(p!==void 0&&w.value>=p&&!x.value)return!0;const{minRef:{value:T}}=c;if(T!==void 0&&w.value<=T&&x.value)return!0}return e?e.disabled.value:!1}}),{mergedDisabledRef:r,mergedSizeRef:m}=t,h=E("Checkbox","-checkbox",Fe,De,o,y);function l(e){if(c&&o.value!==void 0)c.toggleCheckbox(!x.value,o.value);else{const{onChange:g,"onUpdate:checked":p,onUpdateChecked:w}=o,{nTriggerFormInput:T,nTriggerFormChange:D}=t,F=x.value?o.uncheckedValue:o.checkedValue;p&&d(p,F,e),w&&d(w,F,e),g&&d(g,F,e),T(),D(),i.value=F}}function z(e){r.value||l(e)}function R(e){if(!r.value)switch(e.key){case" ":case"Enter":l(e)}}function s(e){e.key===" "&&e.preventDefault()}const I={focus:()=>{var e;(e=u.value)===null||e===void 0||e.focus()},blur:()=>{var e;(e=u.value)===null||e===void 0||e.blur()}},P=ze("Checkbox",f,y),H=B(()=>{const{value:e}=m,{common:{cubicBezierEaseInOut:g},self:{borderRadius:p,color:w,colorChecked:T,colorDisabled:D,colorTableHeader:F,colorTableHeaderModal:O,colorTableHeaderPopover:W,checkMarkColor:Y,checkMarkColorDisabled:Z,border:q,borderFocus:J,borderDisabled:Q,borderChecked:X,boxShadowFocus:ee,textColor:oe,textColorDisabled:re,checkMarkColorDisabledChecked:ae,colorDisabledChecked:ne,borderDisabledChecked:ce,labelPadding:le,labelLineHeight:ie,labelFontWeight:de,[j("fontSize",e)]:te,[j("size",e)]:se}}=h.value;return{"--n-label-line-height":ie,"--n-label-font-weight":de,"--n-size":se,"--n-bezier":g,"--n-border-radius":p,"--n-border":q,"--n-border-checked":X,"--n-border-focus":J,"--n-border-disabled":Q,"--n-border-disabled-checked":ce,"--n-box-shadow-focus":ee,"--n-color":w,"--n-color-checked":T,"--n-color-table":F,"--n-color-table-modal":O,"--n-color-table-popover":W,"--n-color-disabled":D,"--n-color-disabled-checked":ne,"--n-text-color":oe,"--n-text-color-disabled":re,"--n-check-mark-color":Y,"--n-check-mark-color-disabled":Z,"--n-check-mark-color-disabled-checked":ae,"--n-font-size":te,"--n-label-padding":le}}),_=S?Re("checkbox",B(()=>m.value[0]),H,o):void 0;return Object.assign(t,I,{rtlEnabled:P,selfRef:u,mergedClsPrefix:y,mergedDisabled:r,renderedChecked:x,mergedTheme:h,labelId:we(),handleClick:z,handleKeyUp:R,handleKeyDown:s,cssVars:S?void 0:H,themeClass:_?.themeClass,onRender:_?.onRender})},render(){var o;const{$slots:c,renderedChecked:u,mergedDisabled:y,indeterminate:S,privateInsideTable:f,cssVars:v,labelId:i,label:k,mergedClsPrefix:a,focusable:x,handleKeyUp:t,handleKeyDown:r,handleClick:m}=this;(o=this.onRender)===null||o===void 0||o.call(this);const h=me(c.default,l=>k||l?b("span",{class:`${a}-checkbox__label`,id:i},k||l):null);return b("div",{ref:"selfRef",class:[`${a}-checkbox`,this.themeClass,this.rtlEnabled&&`${a}-checkbox--rtl`,u&&`${a}-checkbox--checked`,y&&`${a}-checkbox--disabled`,S&&`${a}-checkbox--indeterminate`,f&&`${a}-checkbox--inside-table`,h&&`${a}-checkbox--show-label`],tabindex:y||!x?void 0:0,role:"checkbox","aria-checked":S?"mixed":u,"aria-labelledby":i,style:v,onKeyup:t,onKeydown:r,onClick:m,onMousedown:()=>{ge("selectstart",window,l=>{l.preventDefault()},{once:!0})}},b("div",{class:`${a}-checkbox-box-wrapper`}," ",b("div",{class:`${a}-checkbox-box`},b(pe,null,{default:()=>this.indeterminate?b("div",{key:"indeterminate",class:`${a}-checkbox-icon`},_e()):b("div",{key:"check",class:`${a}-checkbox-icon`},Me())}),b("div",{class:`${a}-checkbox-box__border`}))),h)}});export{Pe as N,Ae as _,De as c};
