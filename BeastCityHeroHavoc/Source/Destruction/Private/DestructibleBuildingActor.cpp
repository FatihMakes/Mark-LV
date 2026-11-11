#include "DestructibleBuildingActor.h"
#include "WorldStateSubsystem.h"
#include "CityHeatSubsystem.h"
#include "Components/StaticMeshComponent.h"
#include "Kismet/GameplayStatics.h"
#include "Engine/World.h"

ADestructibleBuildingActor::ADestructibleBuildingActor()
{
	PrimaryActorTick.bCanEverTick = false;

	BuildingMesh = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("BuildingMesh"));
	SetRootComponent(BuildingMesh);
	BuildingMesh->SetSimulatePhysics(false);

	CurrentHealth = MaxHealth;
}

void ADestructibleBuildingActor::BeginPlay()
{
	Super::BeginPlay();

	UGameInstance* GameInstance = UGameplayStatics::GetGameInstance(this);
	UWorldStateSubsystem* WorldState = GameInstance ? GameInstance->GetSubsystem<UWorldStateSubsystem>() : nullptr;
	if (WorldState && !BuildingId.IsNone() && WorldState->WasBuildingDestroyed(BuildingId))
	{
		ForceDestroyedState();
	}
}

float ADestructibleBuildingActor::GetMaterialMultiplier() const
{
	switch (MaterialType)
	{
	case EBuildingMaterial::Glass: return 1.5f;
	case EBuildingMaterial::Concrete: return 1.0f;
	case EBuildingMaterial::Steel: return 0.65f;
	default: return 1.0f;
	}
}

float ADestructibleBuildingActor::TakeDamage(float DamageAmount, FDamageEvent const& DamageEvent, AController* EventInstigator, AActor* DamageCauser)
{
	const float Applied = Super::TakeDamage(DamageAmount, DamageEvent, EventInstigator, DamageCauser);
	ApplyBuildingDamage(Applied > 0.0f ? Applied : DamageAmount);
	return Applied;
}

void ADestructibleBuildingActor::ApplyBuildingDamage(float RawDamage)
{
	if (bDestroyed || RawDamage <= 0.0f)
	{
		return;
	}

	const float EffectiveDamage = RawDamage * GetMaterialMultiplier();
	CurrentHealth = FMath::Max(0.0f, CurrentHealth - EffectiveDamage);

	if (CurrentHealth <= 0.0f)
	{
		NotifyHeat(FractureHeatAmount);
		BP_OnFracture();
		EvaluateSupportCollapse();
	}
}

void ADestructibleBuildingActor::EvaluateSupportCollapse()
{
	if (bDestroyed || SupportLinks.Num() == 0)
	{
		// No support graph configured: treat fracture (HP depleted) as immediate collapse.
		if (CurrentHealth <= 0.0f)
		{
			CollapseBuilding();
		}
		return;
	}

	int32 BrokenCount = 0;
	for (const FSupportLink& Link : SupportLinks)
	{
		if (Link.bBroken || Link.LinkedActor == nullptr)
		{
			++BrokenCount;
		}
	}

	const float BrokenRatio = static_cast<float>(BrokenCount) / static_cast<float>(SupportLinks.Num());
	if (BrokenRatio >= CollapseThreshold)
	{
		CollapseBuilding();
	}
}

void ADestructibleBuildingActor::CollapseBuilding()
{
	if (bDestroyed)
	{
		return;
	}

	bDestroyed = true;
	NotifyHeat(CollapseHeatAmount);

	if (UGameInstance* GameInstance = UGameplayStatics::GetGameInstance(this))
	{
		if (UWorldStateSubsystem* WorldState = GameInstance->GetSubsystem<UWorldStateSubsystem>())
		{
			WorldState->MarkBuildingDestroyed(BuildingId);
		}
	}

	BP_OnCollapse();
	OnBuildingCollapsed.Broadcast(this);
}

void ADestructibleBuildingActor::ForceDestroyedState()
{
	bDestroyed = true;
	CurrentHealth = 0.0f;

	if (BuildingMesh)
	{
		BuildingMesh->SetVisibility(false);
		BuildingMesh->SetCollisionEnabled(ECollisionEnabled::NoCollision);
	}
}

void ADestructibleBuildingActor::NotifyHeat(int32 Amount)
{
	if (UWorld* World = GetWorld())
	{
		if (UCityHeatSubsystem* Heat = World->GetSubsystem<UCityHeatSubsystem>())
		{
			Heat->AddHeat(Amount);
		}
	}
}

float ADestructibleBuildingActor::GetHealthNormalized() const
{
	return MaxHealth > 0.0f ? CurrentHealth / MaxHealth : 0.0f;
}
