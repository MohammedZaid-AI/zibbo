import { useState } from "react";

interface User {
  id: string;
  name: string;
  email: string;
  avatarUrl?: string;
}

interface UserCardProps {
  user: User;
  onSelect?: (id: string) => void;
}

export function UserCard({ user, onSelect }: UserCardProps) {
  const [expanded, setExpanded] = useState(false);

  const handleClick = () => {
    setExpanded((prev) => !prev);
    onSelect?.(user.id);
  };

  return (
    <div className="user-card" onClick={handleClick} role="button" tabIndex={0}>
      {user.avatarUrl ? (
        <img className="avatar" src={user.avatarUrl} alt={user.name} />
      ) : (
        <div className="avatar avatar--placeholder">{user.name.charAt(0)}</div>
      )}
      <div className="user-card__body">
        <h3>{user.name}</h3>
        {expanded && <p className="email">{user.email}</p>}
      </div>
    </div>
  );
}

export default UserCard;
