import axios from 'axios'
import addonData from '/src/common'
import React, { useContext, useEffect, useState } from 'react'
import ReactDOM from 'react-dom/client'
import { AddonProvider, AddonContext } from '@ynput/ayon-react-addon-provider'

import PairingList from './PairingList'
import SyncIssuesPanel from './SyncIssuesPanel'

import '@ynput/ayon-react-components/dist/style.css'

import styled from 'styled-components'


const MainContainer = styled.div`
  position: absolute;
  top: 0;
  left: 0;
  height: 100%;
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  padding: 24px 16px;
  box-sizing: border-box;
  overflow-y: auto;

  h1 {
    font-size: 18px;
    padding: 0 0 10px 0;
    border-bottom: 1px solid #ccc;
  }
`

const TabBar = styled.div`
  display: flex;
  gap: 0;
  margin-bottom: 20px;
  border-bottom: 1px solid #ccc;
`

const TabButton = styled.button`
  padding: 10px 18px;
  border: none;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  background: transparent;
  cursor: pointer;
  font-size: 14px;
  color: ${(p) => (p.$active ? '#111' : '#666')};
  font-weight: ${(p) => (p.$active ? 600 : 400)};
  border-bottom-color: ${(p) => (p.$active ? '#333' : 'transparent')};

  &:hover {
    color: #111;
  }
`



const App = () => {
  const accessToken = useContext(AddonContext).accessToken
  const addonName = useContext(AddonContext).addonName
  const addonVersion = useContext(AddonContext).addonVersion
  const [tokenSet, setTokenSet] = useState(false)
  const [activeTab, setActiveTab] = useState('pairing')

  useEffect(() =>{
    if (addonName && addonVersion){
      addonData.addonName = addonName
      addonData.addonVersion = addonVersion
      addonData.baseUrl = `${window.location.origin}/api/addons/${addonName}/${addonVersion}`
      console.log("BaseUrl", addonData.baseUrl)
    }
      
  }, [addonName, addonVersion])


  useEffect(() => {
    if (accessToken && !tokenSet) {
      axios.defaults.headers.common['Authorization'] = `Bearer ${accessToken}`
      setTokenSet(true)
    }
  }, [accessToken, tokenSet])

  if (!tokenSet) {
    return "no token"
  }

  return (
    <>
      <TabBar>
        <TabButton
          type="button"
          $active={activeTab === 'pairing'}
          onClick={() => setActiveTab('pairing')}
        >
          Pairing
        </TabButton>
        <TabButton
          type="button"
          $active={activeTab === 'issues'}
          onClick={() => setActiveTab('issues')}
        >
          Sync issues
        </TabButton>
      </TabBar>
      {activeTab === 'pairing' ? <PairingList /> : <SyncIssuesPanel />}
    </>
  )
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <AddonProvider debug>
      <MainContainer>
        <App />
      </MainContainer>
    </AddonProvider>
  </React.StrictMode>,
)
