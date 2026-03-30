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
  gap: 4px;
  width: 100%;
  max-width: 900px;
  margin-bottom: 20px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.14);
  font-family: inherit;

  @media (prefers-color-scheme: light) {
    border-bottom-color: rgba(0, 0, 0, 0.12);
  }
`

const TabButton = styled.button`
  padding: 10px 18px;
  border: none;
  border-radius: 6px 6px 0 0;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  background: transparent;
  cursor: pointer;
  font-family: inherit;
  font-size: 0.9375rem;
  letter-spacing: 0.02em;
  line-height: 1.3;
  /* Dark UI (AYON Studio default): inactive must stay readable on dark surfaces */
  color: ${(p) =>
    p.$active ? 'rgba(255, 255, 255, 0.98)' : 'rgba(255, 255, 255, 0.72)'};
  font-weight: ${(p) => (p.$active ? 700 : 600)};
  border-bottom-color: ${(p) =>
    p.$active ? 'rgba(255, 255, 255, 0.9)' : 'transparent'};

  &:hover {
    color: rgba(255, 255, 255, 0.95);
  }

  &:focus-visible {
    outline: 2px solid rgba(100, 180, 255, 0.85);
    outline-offset: 2px;
  }

  @media (prefers-color-scheme: light) {
    color: ${(p) => (p.$active ? '#111827' : '#4b5563')};
    font-weight: ${(p) => (p.$active ? 700 : 600)};
    border-bottom-color: ${(p) => (p.$active ? '#111827' : 'transparent')};

    &:hover {
      color: #111827;
    }

    &:focus-visible {
      outline-color: #2563eb;
    }
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
      <TabBar role="tablist" aria-label="Kitsu addon sections">
        <TabButton
          type="button"
          role="tab"
          aria-selected={activeTab === 'pairing'}
          $active={activeTab === 'pairing'}
          onClick={() => setActiveTab('pairing')}
        >
          Pairing
        </TabButton>
        <TabButton
          type="button"
          role="tab"
          aria-selected={activeTab === 'issues'}
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
