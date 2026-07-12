use std::collections::HashMap;

#[derive(Debug, Clone)]
pub struct Item {
    pub id: u64,
    pub name: String,
    pub price: u64,
}

#[derive(Default)]
pub struct Store {
    items: HashMap<u64, Item>,
    next_id: u64,
}

impl Store {
    pub fn new() -> Self {
        Store::default()
    }

    pub fn create(&mut self, name: String, price: u64) -> Item {
        self.next_id += 1;
        let item = Item {
            id: self.next_id,
            name,
            price,
        };
        self.items.insert(item.id, item.clone());
        item
    }

    pub fn get(&self, id: u64) -> Option<&Item> {
        self.items.get(&id)
    }

    pub fn total(&self) -> u64 {
        self.items.values().map(|i| i.price).sum()
    }

    pub fn remove(&mut self, id: u64) -> Option<Item> {
        self.items.remove(&id)
    }
}
