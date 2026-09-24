'use strict';
const assert = require('assert');
const path = require('path');
const collector = require(path.join(__dirname, 'run_mcfarlane_exotic_deterministic.js'));

function testMigrationPreservesLegacyAndKeysRarity() {
  const legacy = {marker:'keep', collections:[{name:'Same',contract:'0x'+'a'.repeat(40),baseline:{listing_count:1}}]};
  const state = collector.migrateWatchState(legacy);
  assert.strictEqual(state.marker, 'keep');
  assert.strictEqual(state.collections[0].rarity, 'Exotic');
  assert.strictEqual(state.collections[0].watch_key, '0x'+'a'.repeat(40)+'|Exotic');
  assert.strictEqual(state.collections[0].baseline.listing_count, 1);
  const fingerprint = JSON.stringify({for_sale:true});
  const migratedCandidate = collector.migrateWatchState({collections:[legacy.collections[0]],pending_exotic_change_candidates:{Same:fingerprint}});
  assert.strictEqual(migratedCandidate.pending_exotic_change_candidates['0x'+'a'.repeat(40)+'|Exotic'], fingerprint);
  assert.strictEqual(migratedCandidate.pending_exotic_change_candidates.Same, undefined);
}

function testSameContractBothRaritiesAreIndependent() {
  const contract = '0x'+'b'.repeat(40);
  const state = collector.migrateWatchState({collections:[
    {name:'Same',contract,rarity:'Exotic',enabled:true},
    {name:'Same',contract,rarity:'Legendary',enabled:true},
  ]});
  const selected = collector.selectCollections(state, {MCFARLANE_WATCH_KEYS: contract+'|Legendary'});
  assert.deepStrictEqual(selected.map(x => x.rarity), ['Legendary']);
  assert.strictEqual(collector.hasExactRarityTrait('TRAITS\nRarity\nLegendary\nBUY NOW', 'Legendary'), true);
  assert.strictEqual(collector.hasExactRarityTrait('TRAITS\nRarity\nExotic\nBUY NOW', 'Legendary'), false);
}

function testReportUsesExplicitRarityLabel() {
  const c = {name:'Same',contract:'0x'+'c'.repeat(40),rarity:'Legendary',watch_key:'0x'+'c'.repeat(40)+'|Legendary',source_url:'https://mcfarlanetoys.digital/',baseline:{for_sale:false,listing_count:0,listings:[]}};
  const text = collector.report({collections:[c]}, {complete:true,failed_collections:[],discord_changes:[{watch_key:c.watch_key,name:c.name,rarity:c.rarity,removed_listings:[]}]});
  assert.match(text, /\[Legendary\]/);
}

for (const test of [testMigrationPreservesLegacyAndKeysRarity, testSameContractBothRaritiesAreIndependent, testReportUsesExplicitRarityLabel]) test();
console.log('rarity collector tests passed');
