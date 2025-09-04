import discord
from discord.ext import commands
from discord import app_commands
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
import json
import asyncio

# ==================== DATA MODELS ====================

class Role(Enum):
    MEMBER = "member"
    AVF = "avf"  # Verified members who can do verification work

class BountyStatus(Enum):
    DRAFT = "draft"
    AWAITING_VERIFICATION = "awaiting_verification"  # Community bounties waiting for pre-verification
    POSTED = "posted"  # Live on the board, can be claimed
    CLAIMED = "claimed"  # Someone is working on it
    AWAITING_POST_VERIFICATION = "awaiting_post_verification"  # Regular bounties waiting for completion verification
    VERIFIED = "verified"  # Completed and verified
    REJECTED = "rejected"  # Failed verification

class BountyType(Enum):
    REGULAR = "regular"  # AVF posts, goes straight to board
    COMMUNITY = "community"  # Members post, needs pre-verification
    RESOURCE = "resource"  # Special handling for Big Iron deliveries

@dataclass
class Member:
    discord_id: int
    role: Role
    time_credits_owed: int = 0
    assigned_bounties: Set[str] = field(default_factory=set)
    
    def can_post_bounty(self) -> bool:
        """Members can post if they don't owe time credits"""
        return self.time_credits_owed <= 0
    
    def can_claim_bounty(self) -> bool:
        """Members can claim if no debt and no current assignments"""
        return self.time_credits_owed <= 0 and len(self.assigned_bounties) == 0
    
    def is_avf(self) -> bool:
        """Check if member has AVF privileges"""
        return self.role == Role.AVF

@dataclass
class Bounty:
    id: str
    creator_id: int
    bounty_type: BountyType
    status: BountyStatus
    title: str
    description: str
    assigned_to: Optional[int] = None
    verifier_id: Optional[int] = None  # Who's doing the verification
    message_id: Optional[int] = None  # Discord message ID for reactions
    
    def can_be_claimed(self) -> bool:
        """Check if bounty is available for claiming"""
        return self.status == BountyStatus.POSTED

# ==================== BOT SETUP ====================

class BountyBot(commands.Bot):
    def __init__(self):
        # Discord bot setup - you'll need these permissions:
        # - Send Messages, Manage Messages, Add Reactions, Use Slash Commands
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        
        super().__init__(command_prefix='!', intents=intents)
        
        # Data storage - in production, use a real database
        self.members: Dict[int, Member] = {}
        self.bounties: Dict[str, Bounty] = {}
        self.bounty_counter = 0
        
        # Channel IDs - SET THESE TO YOUR ACTUAL DISCORD CHANNELS
        self.BOUNTY_BOARD_CHANNEL = None  # Where posted bounties go
        self.VERIFICATION_CHANNEL = None  # Where verification requests go
        self.LOG_CHANNEL = None  # For bot logs/notifications
        
        # Reaction emojis - customize these
        self.MINE_EMOJI = "⛏️"  # For claiming bounties
        self.VERIFY_EMOJI = "✅"  # For requesting verification
        self.APPROVE_EMOJI = "👍"  # For AVF approval
        self.REJECT_EMOJI = "👎"  # For AVF rejection

    async def setup_hook(self):
        """Called when bot starts up"""
        await self.tree.sync()
        print("Bounty bot is ready!")

# ==================== MEMBER MANAGEMENT ====================

    def get_or_create_member(self, discord_id: int) -> Member:
        """Get member or create new one with default role"""
        if discord_id not in self.members:
            # TODO: You might want to check Discord roles here to auto-assign AVF
            self.members[discord_id] = Member(discord_id, Role.MEMBER)
        return self.members[discord_id]

    @app_commands.command(name="register", description="Register as a member")
    async def register(self, interaction: discord.Interaction):
        """Let users register themselves"""
        member = self.get_or_create_member(interaction.user.id)
        await interaction.response.send_message(
            f"Welcome! You're registered as: {member.role.value}\n"
            f"Time credits owed: {member.time_credits_owed}", 
            ephemeral=True
        )

    @app_commands.command(name="promote", description="Promote a member to AVF (AVF only)")
    @app_commands.describe(user="User to promote")
    async def promote_member(self, interaction: discord.Interaction, user: discord.Member):
        """AVF can promote members"""
        promoter = self.get_or_create_member(interaction.user.id)
        
        if not promoter.is_avf():
            await interaction.response.send_message("Only AVF members can promote others.", ephemeral=True)
            return
        
        target = self.get_or_create_member(user.id)
        target.role = Role.AVF
        
        await interaction.response.send_message(f"{user.mention} has been promoted to AVF!")

# ==================== BOUNTY POSTING ====================

    @app_commands.command(name="post_bounty", description="Post a new bounty")
    @app_commands.describe(
        title="Short title for the bounty",
        description="Detailed description of what needs to be done",
        bounty_type="Type of bounty (regular for AVF, community for members)"
    )
    @app_commands.choices(bounty_type=[
        app_commands.Choice(name="Regular (AVF only)", value="regular"),
        app_commands.Choice(name="Community (needs verification)", value="community"),
        app_commands.Choice(name="Resource (Big Iron)", value="resource")
    ])
    async def post_bounty(self, interaction: discord.Interaction, title: str, description: str, bounty_type: str):
        """Main bounty posting command"""
        member = self.get_or_create_member(interaction.user.id)
        
        # Check if member can post
        if not member.can_post_bounty():
            await interaction.response.send_message(
                f"You owe {member.time_credits_owed} time credits. Clear your debt first!", 
                ephemeral=True
            )
            return
        
        # Create bounty ID
        self.bounty_counter += 1
        bounty_id = f"bounty_{self.bounty_counter}"
        
        # Determine initial status based on type and poster role
        bounty_type_enum = BountyType(bounty_type)
        
        if bounty_type_enum == BountyType.REGULAR:
            if not member.is_avf():
                await interaction.response.send_message("Only AVF members can post regular bounties.", ephemeral=True)
                return
            initial_status = BountyStatus.POSTED  # AVF bounties go straight to board
        elif bounty_type_enum == BountyType.COMMUNITY:
            initial_status = BountyStatus.AWAITING_VERIFICATION  # Needs pre-verification
        else:  # RESOURCE
            initial_status = BountyStatus.AWAITING_VERIFICATION  # Resources also need verification
        
        # Create the bounty
        bounty = Bounty(
            id=bounty_id,
            creator_id=interaction.user.id,
            bounty_type=bounty_type_enum,
            status=initial_status,
            title=title,
            description=description
        )
        
        self.bounties[bounty_id] = bounty
        
        # Post to appropriate channel
        if initial_status == BountyStatus.POSTED:
            await self._post_to_board(bounty, interaction)
        else:
            await self._post_for_verification(bounty, interaction)

    async def _post_to_board(self, bounty: Bounty, interaction: discord.Interaction):
        """Post bounty to the main board where it can be claimed"""
        if not self.BOUNTY_BOARD_CHANNEL:
            await interaction.response.send_message("Bounty board channel not configured!", ephemeral=True)
            return
        
        channel = self.get_channel(self.BOUNTY_BOARD_CHANNEL)
        if not channel:
            await interaction.response.send_message("Could not find bounty board channel!", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"🎯 {bounty.title}",
            description=bounty.description,
            color=0x00ff00
        )
        embed.add_field(name="Bounty ID", value=bounty.id, inline=True)
        embed.add_field(name="Type", value=bounty.bounty_type.value, inline=True)
        embed.add_field(name="Status", value="Available to claim", inline=True)
        embed.set_footer(text=f"React with {self.MINE_EMOJI} to claim this bounty")
        
        message = await channel.send(embed=embed)
        await message.add_reaction(self.MINE_EMOJI)
        
        bounty.message_id = message.id
        
        await interaction.response.send_message(f"Bounty {bounty.id} posted to the board!")

    async def _post_for_verification(self, bounty: Bounty, interaction: discord.Interaction):
        """Post community bounty for pre-verification"""
        if not self.VERIFICATION_CHANNEL:
            await interaction.response.send_message("Verification channel not configured!", ephemeral=True)
            return
        
        channel = self.get_channel(self.VERIFICATION_CHANNEL)
        if not channel:
            await interaction.response.send_message("Could not find verification channel!", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"📋 Verification Request: {bounty.title}",
            description=bounty.description,
            color=0xffaa00
        )
        embed.add_field(name="Bounty ID", value=bounty.id, inline=True)
        embed.add_field(name="Type", value=bounty.bounty_type.value, inline=True)
        embed.add_field(name="Submitted by", value=f"<@{bounty.creator_id}>", inline=True)
        embed.set_footer(text=f"AVF: React {self.APPROVE_EMOJI} to approve, {self.REJECT_EMOJI} to reject")
        
        message = await channel.send(embed=embed)
        await message.add_reaction(self.APPROVE_EMOJI)
        await message.add_reaction(self.REJECT_EMOJI)
        
        bounty.message_id = message.id
        
        await interaction.response.send_message(f"Bounty {bounty.id} submitted for verification!")

# ==================== CLAIMING BOUNTIES ====================

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        """Handle reaction-based claiming and verification"""
        if user.bot:
            return
        
        # Find bounty by message ID
        bounty = None
        for b in self.bounties.values():
            if b.message_id == reaction.message.id:
                bounty = b
                break
        
        if not bounty:
            return
        
        member = self.get_or_create_member(user.id)
        
        # Handle claiming with MINE emoji
        if str(reaction.emoji) == self.MINE_EMOJI:
            await self._handle_claim_reaction(bounty, member, reaction)
        
        # Handle verification reactions (AVF only)
        elif str(reaction.emoji) in [self.APPROVE_EMOJI, self.REJECT_EMOJI]:
            await self._handle_verification_reaction(bounty, member, reaction)
        
        # Handle completion verification request
        elif str(reaction.emoji) == self.VERIFY_EMOJI:
            await self._handle_completion_verification_request(bounty, member, reaction)

    async def _handle_claim_reaction(self, bounty: Bounty, member: Member, reaction):
        """Handle someone trying to claim a bounty"""
        if not bounty.can_be_claimed():
            await reaction.remove(member.discord_id)
            return
        
        if not member.can_claim_bounty():
            # Remove their reaction and DM them why
            await reaction.remove(member.discord_id)
            user = self.get_user(member.discord_id)
            if user:
                reason = "you have outstanding time credits" if member.time_credits_owed > 0 else "you already have an assigned bounty"
                await user.send(f"Cannot claim bounty {bounty.id}: {reason}")
            return
        
        # Successful claim
        bounty.assigned_to = member.discord_id
        bounty.status = BountyStatus.CLAIMED
        member.assigned_bounties.add(bounty.id)
        
        # Update the message
        embed = reaction.message.embeds[0]
        embed.color = 0xff6600  # Orange for claimed
        embed.set_field_at(2, name="Status", value=f"Claimed by <@{member.discord_id}>", inline=True)
        embed.set_footer(text=f"React with {self.VERIFY_EMOJI} when complete")
        
        await reaction.message.edit(embed=embed)
        await reaction.message.add_reaction(self.VERIFY_EMOJI)
        
        # Notify in log channel
        if self.LOG_CHANNEL:
            log_channel = self.get_channel(self.LOG_CHANNEL)
            if log_channel:
                await log_channel.send(f"🎯 Bounty {bounty.id} claimed by <@{member.discord_id}>")

    async def _handle_verification_reaction(self, bounty: Bounty, member: Member, reaction):
        """Handle AVF verification of community bounties"""
        if not member.is_avf():
            await reaction.remove(member.discord_id)
            return
        
        if bounty.status != BountyStatus.AWAITING_VERIFICATION:
            return
        
        approved = str(reaction.emoji) == self.APPROVE_EMOJI
        
        if approved:
            bounty.status = BountyStatus.POSTED
            bounty.verifier_id = member.discord_id
            
            # Move to bounty board
            if self.BOUNTY_BOARD_CHANNEL:
                await self._post_to_board(bounty, None)
            
            # Update verification message
            embed = reaction.message.embeds[0]
            embed.color = 0x00ff00
            embed.title = f"✅ APPROVED: {bounty.title}"
            embed.add_field(name="Verified by", value=f"<@{member.discord_id}>", inline=True)
            await reaction.message.edit(embed=embed)
            
        else:  # Rejected
            bounty.status = BountyStatus.REJECTED
            
            embed = reaction.message.embeds[0]
            embed.color = 0xff0000
            embed.title = f"❌ REJECTED: {bounty.title}"
            embed.add_field(name="Rejected by", value=f"<@{member.discord_id}>", inline=True)
            await reaction.message.edit(embed=embed)
            
            # TODO: You might want to add time credits penalty or notification to creator

    async def _handle_completion_verification_request(self, bounty: Bounty, member: Member, reaction):
        """Handle request for post-completion verification"""
        if bounty.status != BountyStatus.CLAIMED or bounty.assigned_to != member.discord_id:
            await reaction.remove(member.discord_id)
            return
        
        bounty.status = BountyStatus.AWAITING_POST_VERIFICATION
        
        # Create verification request
        if self.VERIFICATION_CHANNEL:
            channel = self.get_channel(self.VERIFICATION_CHANNEL)
            if channel:
                embed = discord.Embed(
                    title=f"🔍 Completion Verification: {bounty.title}",
                    description=f"<@{member.discord_id}> claims to have completed this bounty.",
                    color=0x9966cc
                )
                embed.add_field(name="Bounty ID", value=bounty.id, inline=True)
                embed.add_field(name="Original Description", value=bounty.description[:500], inline=False)
                embed.set_footer(text=f"AVF: React {self.APPROVE_EMOJI} to verify completion, {self.REJECT_EMOJI} to reject")
                
                message = await channel.send(embed=embed)
                await message.add_reaction(self.APPROVE_EMOJI)
                await message.add_reaction(self.REJECT_EMOJI)

# ==================== MANUAL COMMANDS ====================

    @app_commands.command(name="my_bounties", description="Show your assigned bounties")
    async def my_bounties(self, interaction: discord.Interaction):
        """Let members check their current assignments"""
        member = self.get_or_create_member(interaction.user.id)
        
        if not member.assigned_bounties:
            await interaction.response.send_message("You have no assigned bounties.", ephemeral=True)
            return
        
        bounty_list = []
        for bounty_id in member.assigned_bounties:
            bounty = self.bounties.get(bounty_id)
            if bounty:
                bounty_list.append(f"**{bounty.id}**: {bounty.title} ({bounty.status.value})")
        
        embed = discord.Embed(
            title="Your Assigned Bounties",
            description="\n".join(bounty_list),
            color=0x0099ff
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="list_bounties", description="List all bounties (with optional filter)")
    @app_commands.describe(status="Filter by status")
    async def list_bounties(self, interaction: discord.Interaction, status: Optional[str] = None):
        """List bounties, optionally filtered by status"""
        bounties = list(self.bounties.values())
        
        if status:
            try:
                status_enum = BountyStatus(status.lower())
                bounties = [b for b in bounties if b.status == status_enum]
            except ValueError:
                await interaction.response.send_message(f"Invalid status. Valid options: {[s.value for s in BountyStatus]}", ephemeral=True)
                return
        
        if not bounties:
            await interaction.response.send_message("No bounties found.", ephemeral=True)
            return
        
        # Group by status for better organization
        status_groups = {}
        for bounty in bounties:
            if bounty.status not in status_groups:
                status_groups[bounty.status] = []
            status_groups[bounty.status].append(bounty)
        
        embed = discord.Embed(title="Bounty List", color=0x0099ff)
        
        for status, bounty_list in status_groups.items():
            value = "\n".join([f"**{b.id}**: {b.title}" for b in bounty_list[:5]])  # Limit to 5 per status
            if len(bounty_list) > 5:
                value += f"\n... and {len(bounty_list) - 5} more"
            embed.add_field(name=f"{status.value.title()} ({len(bounty_list)})", value=value, inline=False)
        
        await interaction.response.send_message(embed=embed)

# ==================== ADMIN COMMANDS ====================

    @app_commands.command(name="adjust_credits", description="Adjust member's time credits (AVF only)")
    @app_commands.describe(user="User to adjust", amount="Amount to add/subtract")
    async def adjust_credits(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        """AVF can adjust time credits"""
        admin = self.get_or_create_member(interaction.user.id)
        
        if not admin.is_avf():
            await interaction.response.send_message("Only AVF members can adjust credits.", ephemeral=True)
            return
        
        target = self.get_or_create_member(user.id)
        target.time_credits_owed += amount
        
        await interaction.response.send_message(
            f"Adjusted {user.mention}'s time credits by {amount}. "
            f"New balance: {target.time_credits_owed}"
        )

# ==================== BOT TOKEN AND STARTUP ====================

# To run this bot:
# 1. Create a Discord application at https://discord.com/developers/applications
# 2. Create a bot user and get the token
# 3. Set the channel IDs above to your actual Discord channels
# 4. Install discord.py: pip install discord.py
# 5. Run with: python bot.py

if __name__ == "__main__":
    # IMPORTANT: Replace with your actual bot token
    TOKEN = "YOUR_BOT_TOKEN_HERE"
    
    bot = BountyBot()
    
    # Set your channel IDs here
    bot.BOUNTY_BOARD_CHANNEL = 123456789  # Replace with actual channel ID
    bot.VERIFICATION_CHANNEL = 123456789   # Replace with actual channel ID  
    bot.LOG_CHANNEL = 123456789           # Replace with actual channel ID
    
    bot.run(TOKEN)