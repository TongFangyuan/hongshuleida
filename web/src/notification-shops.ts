import './styles/notification-shops.css'

type Shop = { shop_id: string; shop_name: string }
type NotificationShops = { shops: Shop[]; selected_shop_ids: string[] }

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(`/api${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!response.ok) throw new Error(await response.text())
  return response.json()
}

const errorDetail = (error: unknown): string => {
  const text = error instanceof Error ? error.message : String(error)
  try { return String(JSON.parse(text).detail || text) } catch { return text }
}

function currentSettingsSection(): HTMLElement | undefined {
  return [...document.querySelectorAll('section')].find(section => section.querySelector('h1')?.textContent === '设置') as HTMLElement | undefined
}

function addShopRow(list: HTMLElement, shop: Shop, selected: Set<string>): void {
  const row = document.createElement('label')
  row.className = 'notification-shop-row'
  row.dataset.shopId = shop.shop_id
  row.dataset.search = shop.shop_name.toLocaleLowerCase()

  const input = document.createElement('input')
  input.type = 'checkbox'
  input.checked = selected.has(shop.shop_id)
  input.addEventListener('change', () => input.checked ? selected.add(shop.shop_id) : selected.delete(shop.shop_id))

  const name = document.createElement('span')
  name.textContent = shop.shop_name
  row.append(input, name)
  list.append(row)
}

async function mountNotificationShops(): Promise<void> {
  const section = currentSettingsSection()
  if (!section || section.querySelector('[data-notification-shops]')) return

  const panel = document.createElement('div')
  panel.className = 'notification-shops'
  panel.dataset.notificationShops = 'true'
  const title = document.createElement('h2')
  title.textContent = '通知店铺'
  const description = document.createElement('p')
  description.textContent = '仅勾选的店铺会在正式定时采集完成后收到销量时报。'
  const search = document.createElement('input')
  search.type = 'search'
  search.placeholder = '搜索店铺'
  const actions = document.createElement('div')
  actions.className = 'actions'
  const selectAll = document.createElement('button')
  selectAll.type = 'button'
  selectAll.className = 'secondary'
  selectAll.textContent = '全选'
  const clear = document.createElement('button')
  clear.type = 'button'
  clear.className = 'secondary'
  clear.textContent = '取消全选'
  const save = document.createElement('button')
  save.type = 'button'
  save.textContent = '保存通知店铺'
  actions.append(selectAll, clear, save)
  const status = document.createElement('p')
  status.className = 'notification-shop-status'
  const list = document.createElement('div')
  list.className = 'notification-shop-list'
  panel.append(title, description, search, actions, status, list)
  section.querySelector('.form')?.insertAdjacentElement('afterend', panel)

  try {
    const data = await api<NotificationShops>('/notification-shops')
    const selected = new Set(data.selected_shop_ids)
    const renderStatus = () => {
      status.textContent = selected.size ? `已选择 ${selected.size} 家店铺` : '未选择店铺，正式定时采集不会发送销量时报。'
    }

    for (const shop of data.shops) addShopRow(list, shop, selected)
    renderStatus()

    search.addEventListener('input', () => {
      const keyword = search.value.trim().toLocaleLowerCase()
      for (const row of list.children) {
        const item = row as HTMLElement
        item.hidden = !(item.dataset.search || '').includes(keyword)
      }
    })
    selectAll.addEventListener('click', () => {
      for (const row of list.children) {
        const item = row as HTMLElement
        if (item.hidden) continue
        const input = item.querySelector('input') as HTMLInputElement
        input.checked = true
        selected.add(item.dataset.shopId || '')
      }
      renderStatus()
    })
    clear.addEventListener('click', () => {
      for (const row of list.children) {
        const item = row as HTMLElement
        if (item.hidden) continue
        const input = item.querySelector('input') as HTMLInputElement
        input.checked = false
        selected.delete(item.dataset.shopId || '')
      }
      renderStatus()
    })
    list.addEventListener('change', renderStatus)
    save.addEventListener('click', async () => {
      save.disabled = true
      try {
        const result = await api<NotificationShops>('/notification-shops', {
          method: 'PUT',
          body: JSON.stringify({ shop_ids: [...selected] }),
        })
        selected.clear()
        result.selected_shop_ids.forEach(id => selected.add(id))
        renderStatus()
        status.textContent += '，已保存。'
      } catch (error) {
        status.textContent = errorDetail(error)
      } finally {
        save.disabled = false
      }
    })
  } catch (error) {
    status.textContent = `无法加载通知店铺：${errorDetail(error)}`
  }
}

new MutationObserver(() => { void mountNotificationShops() }).observe(document.documentElement, { childList: true, subtree: true })
void mountNotificationShops()
