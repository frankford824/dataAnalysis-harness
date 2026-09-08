// Add one keyboard contract to existing Naive tabs without replacing their panels.
export const accessibleTabs = {
  mounted(root) {
    const tabs=()=>[...root.querySelectorAll('.n-tabs-tab')].filter(tab=>tab.closest('.n-tabs')===root && tab.textContent.trim())
    const sync=()=>{
      const items=tabs()
      const nav=root.querySelector('.n-tabs-nav');if(nav)nav.setAttribute('role','tablist')
      for(const tab of items){
        const selected=tab.classList.contains('n-tabs-tab--active'),disabled=tab.classList.contains('n-tabs-tab--disabled')
        tab.setAttribute('role','tab');tab.setAttribute('aria-selected',String(selected));tab.setAttribute('aria-disabled',String(disabled));tab.tabIndex=selected&&!disabled?0:-1
      }
    }
    const keydown=event=>{
      const target=event.target.closest('.n-tabs-tab')
      if(!target || target.closest('.n-tabs')!==root)return
      const available=tabs().filter(tab=>!tab.classList.contains('n-tabs-tab--disabled'))
      const index=available.indexOf(target);if(index<0)return
      let next=index
      if(event.key==='ArrowRight')next=(index+1)%available.length
      else if(event.key==='ArrowLeft')next=(index-1+available.length)%available.length
      else if(event.key==='Home')next=0
      else if(event.key==='End')next=available.length-1
      else if(!['Enter',' '].includes(event.key))return
      event.preventDefault();event.stopPropagation();available[next].click();available[next].focus()
    }
    const observer=new MutationObserver(sync)
    observer.observe(root,{subtree:true,childList:true,attributes:true,attributeFilter:['class']})
    root.addEventListener('keydown',keydown);sync()
    root.__ledgerTabCleanup=()=>{observer.disconnect();root.removeEventListener('keydown',keydown)}
  },
  unmounted(root){root.__ledgerTabCleanup?.();delete root.__ledgerTabCleanup},
}
