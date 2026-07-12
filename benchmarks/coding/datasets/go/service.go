package orders

import (
	"context"
	"errors"
	"fmt"
)

// ErrNotFound is returned when an order does not exist.
var ErrNotFound = errors.New("order not found")

// Store abstracts order persistence.
type Store interface {
	Get(ctx context.Context, id string) (*Order, error)
	Save(ctx context.Context, o *Order) error
}

// Order is a customer order with line items.
type Order struct {
	ID    string
	Lines []Line
}

// Line is a single item within an order.
type Line struct {
	SKU   string
	Price int
	Qty   int
}

// Total returns the order total in cents.
func (o *Order) Total() int {
	total := 0
	for _, l := range o.Lines {
		total += l.Price * l.Qty
	}
	return total
}

// Service coordinates order operations.
type Service struct {
	store Store
}

// NewService constructs a Service.
func NewService(store Store) *Service {
	return &Service{store: store}
}

// Place validates and persists a new order.
func (s *Service) Place(ctx context.Context, o *Order) error {
	if len(o.Lines) == 0 {
		return fmt.Errorf("order %s has no lines", o.ID)
	}
	return s.store.Save(ctx, o)
}
