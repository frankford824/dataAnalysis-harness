import{x as W,b8 as t,y as o,z as C,Z as v,$ as A,az as F,aA as I,A as D,C as G,G as K,H as z,a1 as N,a2 as Z,p as f,I as x}from"./index-fM-1Y40E.js";const q={thPaddingSmall:"6px",thPaddingMedium:"12px",thPaddingLarge:"12px",tdPaddingSmall:"6px",tdPaddingMedium:"12px",tdPaddingLarge:"12px"};function J(e){const{dividerColor:r,cardColor:a,modalColor:s,popoverColor:l,tableHeaderColor:c,tableColorStriped:b,textColor1:p,textColor2:g,borderRadius:n,fontWeightStrong:d,lineHeight:i,fontSizeSmall:h,fontSizeMedium:m,fontSizeLarge:u}=e;return Object.assign(Object.assign({},q),{fontSizeSmall:h,fontSizeMedium:m,fontSizeLarge:u,lineHeight:i,borderRadius:n,borderColor:t(a,r),borderColorModal:t(s,r),borderColorPopover:t(l,r),tdColor:a,tdColorModal:s,tdColorPopover:l,tdColorStriped:t(a,b),tdColorStripedModal:t(s,b),tdColorStripedPopover:t(l,b),thColor:t(a,c),thColorModal:t(s,c),thColorPopover:t(l,c),thTextColor:p,tdTextColor:g,thFontWeight:d})}const Q={common:W,self:J},U=o([C("table",`
 font-size: var(--n-font-size);
 font-variant-numeric: tabular-nums;
 line-height: var(--n-line-height);
 width: 100%;
 border-radius: var(--n-border-radius) var(--n-border-radius) 0 0;
 text-align: left;
 border-collapse: separate;
 border-spacing: 0;
 overflow: hidden;
 background-color: var(--n-td-color);
 border-color: var(--n-merged-border-color);
 transition:
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier),
 color .3s var(--n-bezier);
 --n-merged-border-color: var(--n-border-color);
 `,[o("th",`
 white-space: nowrap;
 transition:
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier),
 color .3s var(--n-bezier);
 text-align: inherit;
 padding: var(--n-th-padding);
 vertical-align: inherit;
 text-transform: none;
 border: 0px solid var(--n-merged-border-color);
 font-weight: var(--n-th-font-weight);
 color: var(--n-th-text-color);
 background-color: var(--n-th-color);
 border-bottom: 1px solid var(--n-merged-border-color);
 border-right: 1px solid var(--n-merged-border-color);
 `,[o("&:last-child",`
 border-right: 0px solid var(--n-merged-border-color);
 `)]),o("td",`
 transition:
 background-color .3s var(--n-bezier),
 border-color .3s var(--n-bezier),
 color .3s var(--n-bezier);
 padding: var(--n-td-padding);
 color: var(--n-td-text-color);
 background-color: var(--n-td-color);
 border: 0px solid var(--n-merged-border-color);
 border-right: 1px solid var(--n-merged-border-color);
 border-bottom: 1px solid var(--n-merged-border-color);
 `,[o("&:last-child",`
 border-right: 0px solid var(--n-merged-border-color);
 `)]),v("bordered",`
 border: 1px solid var(--n-merged-border-color);
 border-radius: var(--n-border-radius);
 `,[o("tr",[o("&:last-child",[o("td",`
 border-bottom: 0 solid var(--n-merged-border-color);
 `)])])]),v("single-line",[o("th",`
 border-right: 0px solid var(--n-merged-border-color);
 `),o("td",`
 border-right: 0px solid var(--n-merged-border-color);
 `)]),v("single-column",[o("tr",[o("&:not(:last-child)",[o("td",`
 border-bottom: 0px solid var(--n-merged-border-color);
 `)])])]),v("striped",[o("tr:nth-of-type(even)",[o("td","background-color: var(--n-td-color-striped)")])]),A("bottom-bordered",[o("tr",[o("&:last-child",[o("td",`
 border-bottom: 0px solid var(--n-merged-border-color);
 `)])])])]),F(C("table",`
 background-color: var(--n-td-color-modal);
 --n-merged-border-color: var(--n-border-color-modal);
 `,[o("th",`
 background-color: var(--n-th-color-modal);
 `),o("td",`
 background-color: var(--n-td-color-modal);
 `)])),I(C("table",`
 background-color: var(--n-td-color-popover);
 --n-merged-border-color: var(--n-border-color-popover);
 `,[o("th",`
 background-color: var(--n-th-color-popover);
 `),o("td",`
 background-color: var(--n-td-color-popover);
 `)]))]),X=Object.assign(Object.assign({},z.props),{bordered:{type:Boolean,default:!0},bottomBordered:{type:Boolean,default:!0},singleLine:{type:Boolean,default:!0},striped:Boolean,singleColumn:Boolean,size:String}),oo=D({name:"Table",props:X,setup(e){const{mergedClsPrefixRef:r,inlineThemeDisabled:a,mergedRtlRef:s,mergedComponentPropsRef:l}=K(e),c=f(()=>{var d,i;return e.size||((i=(d=l?.value)===null||d===void 0?void 0:d.Table)===null||i===void 0?void 0:i.size)||"medium"}),b=z("Table","-table",U,Q,e,r),p=N("Table",s,r),g=f(()=>{const d=c.value,{self:{borderColor:i,tdColor:h,tdColorModal:m,tdColorPopover:u,thColor:P,thColorModal:S,thColorPopover:M,thTextColor:R,tdTextColor:k,borderRadius:T,thFontWeight:B,lineHeight:$,borderColorModal:y,borderColorPopover:_,tdColorStriped:w,tdColorStripedModal:L,tdColorStripedPopover:H,[x("fontSize",d)]:O,[x("tdPadding",d)]:j,[x("thPadding",d)]:E},common:{cubicBezierEaseInOut:V}}=b.value;return{"--n-bezier":V,"--n-td-color":h,"--n-td-color-modal":m,"--n-td-color-popover":u,"--n-td-text-color":k,"--n-border-color":i,"--n-border-color-modal":y,"--n-border-color-popover":_,"--n-border-radius":T,"--n-font-size":O,"--n-th-color":P,"--n-th-color-modal":S,"--n-th-color-popover":M,"--n-th-font-weight":B,"--n-th-text-color":R,"--n-line-height":$,"--n-td-padding":j,"--n-th-padding":E,"--n-td-color-striped":w,"--n-td-color-striped-modal":L,"--n-td-color-striped-popover":H}}),n=a?Z("table",f(()=>c.value[0]),g,e):void 0;return{rtlEnabled:p,mergedClsPrefix:r,cssVars:a?void 0:g,themeClass:n?.themeClass,onRender:n?.onRender}},render(){var e;const{mergedClsPrefix:r}=this;return(e=this.onRender)===null||e===void 0||e.call(this),G("table",{class:[`${r}-table`,this.themeClass,{[`${r}-table--rtl`]:this.rtlEnabled,[`${r}-table--bottom-bordered`]:this.bottomBordered,[`${r}-table--bordered`]:this.bordered,[`${r}-table--single-line`]:this.singleLine,[`${r}-table--single-column`]:this.singleColumn,[`${r}-table--striped`]:this.striped}],style:this.cssVars},this.$slots)}});export{oo as _};
