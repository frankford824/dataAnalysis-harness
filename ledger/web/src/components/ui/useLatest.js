import { onUnmounted } from 'vue'
import { latestRequest } from '../commissionRequest'
export function useLatest() {
  const request=latestRequest()
  onUnmounted(request.cancel)
  return request
}
