import './style.css'
import { Cable } from 'lucide-vue-next'
import Accounts from './views/Accounts.vue'
import { host } from './host'

host.registerPlugin({
  id: 'onebot_v11',
  settingsPanels: [
    {
      id: 'onebot-v11',
      title: 'OneBot v11',
      icon: Cable,
      group: '适配器',
      files: ['onebot_v11.yml', 'onebot_v11.yaml'],
      component: Accounts,
    },
  ],
})
